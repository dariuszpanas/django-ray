"""Tests for the live workflow reporting-policy benchmark command."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from django_ray.models import (
    RayTaskExecution,
    TaskAttempt,
    TaskState,
    WorkflowProgressNodeDetail,
    WorkflowProgressNodeState,
    WorkflowProgressRunStorage,
    WorkflowProgressTopologyCollection,
    WorkflowProgressTopologyManifest,
    WorkflowProgressTopologyManifestPage,
    WorkflowProgressTopologyPage,
    WorkflowProgressTopologySlot,
)
from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.workflow_plans import (
    PLAN_DOMAIN_SEPARATOR,
    PLAN_FORMAT,
    PLAN_FORMAT_VERSION,
)
from django_ray.workflow_progress_summary import serialize_workflow_progress_summary
from testproject.management.commands import (
    django_ray_benchmark_workflow_reporting as benchmark,
)
from tests.workflow_progress_summary_helpers import (
    terminal_only_workflow_progress_summary,
)


def _result_data() -> str:
    return json.dumps(
        {
            "engine": "django-ray-workflow",
            "shape": benchmark.EXPECTED_WORKFLOW_SHAPE,
            "durability_boundary": "single RayTaskExecution",
            "total_leaf_tasks": 3,
            "branches": [
                {
                    "engine": "django-ray-workflow",
                    "durability_boundary": "single RayTaskExecution",
                    "branch": "fast",
                    "leaf_tasks": 2,
                    "total_leaf_seconds": 0.02,
                    "leaf_wall_seconds": 0.02,
                    "items": [{"private": "not emitted"}, {"private": "not emitted"}],
                },
                {
                    "engine": "django-ray-workflow",
                    "durability_boundary": "single RayTaskExecution",
                    "branch": "slow",
                    "leaf_tasks": 1,
                    "total_leaf_seconds": 0.02,
                    "leaf_wall_seconds": 0.02,
                    "items": [{"private": "not emitted"}],
                },
            ],
            "workflow_elapsed_seconds": 0.08,
        }
    )


def _selection(policy: str) -> str:
    return json.dumps(
        {
            "plan_selection_format": "django-ray.workflow-plan-selection",
            "plan_selection_format_version": 2,
            "requested_policy": "auto",
            "reporting_policy": policy,
            "selected_strategy": "dynamic_tasks",
            "eligible_strategies": ["dynamic_tasks"],
            "rejections": [],
            "total_rejections": 0,
            "rejections_truncated": False,
        }
    )


def _plan() -> tuple[str, str]:
    manifest = {
        "plan_format": PLAN_FORMAT,
        "plan_format_version": PLAN_FORMAT_VERSION,
        "definition": {
            "name": benchmark.EXPECTED_WORKFLOW_DEFINITION,
            "revision": f"sha256:{'b' * 64}",
        },
        "topology": {"class": "dynamic"},
        "nodes": [{"node_id": f"plan-{index}"} for index in range(13)],
        "edges": [
            *[{"source": f"plan-{index}", "target": f"plan-{index + 1}"} for index in range(12)],
            {"source": "plan-0", "target": "plan-2"},
        ],
    }
    serialized = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    fingerprint = (
        "sha256:" + hashlib.sha256(PLAN_DOMAIN_SEPARATOR + serialized.encode("utf-8")).hexdigest()
    )
    return serialized, fingerprint


def _refresh_retained_bytes(snapshot: dict[str, Any]) -> None:
    nodes = []
    for value in snapshot["graph"]["nodes"]:
        node = deepcopy(value)
        node["dependencies"] = []
        nodes.append(node)
    retained = {
        "edges": snapshot["graph"]["edges"],
        "nodes": nodes,
        "plan": snapshot["plan"],
        "recent_events": snapshot["recent_events"],
    }
    snapshot["ingress"]["retained_bytes"] = len(
        json.dumps(
            retained,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _ingress_progress_for(execution: RayTaskExecution) -> str:
    expected_nodes, expected_edges = benchmark._expected_dynamic_topology(
        fast_items=2,
        slow_items=1,
    )
    edges = [
        {"source": f"node-{index}", "target": f"node-{index + 1}"}
        for index in range(expected_nodes - 1)
    ]
    edges.extend(
        [
            {"source": "node-0", "target": "node-2"},
            {"source": "node-1", "target": "node-3"},
        ]
    )
    inbound: dict[str, list[str]] = {f"node-{index}": [] for index in range(expected_nodes)}
    for edge in edges:
        inbound[edge["target"]].append(edge["source"])
    accepted_by_kind = dict.fromkeys(benchmark._EXPECTED_INGRESS_KINDS, 0)
    accepted_by_kind.update(
        initialized=1,
        node_registered=expected_nodes,
        edges_registered=expected_nodes - 1,
        submitted=expected_nodes,
        started=expected_nodes,
        application_progress=3,
        completed=expected_nodes,
    )
    accepted = sum(accepted_by_kind.values())
    identity = benchmark._run_identity(execution)
    snapshot: dict[str, Any] = {
        "schema_version": 2,
        "workflow_id": f"django-ray:{execution.pk}",
        "run_identity": identity.as_dict(),
        "plan": {
            "plan_format": PLAN_FORMAT,
            "plan_format_version": PLAN_FORMAT_VERSION,
            "fingerprint": execution.workflow_plan_fingerprint,
            "definition_name": benchmark.EXPECTED_WORKFLOW_DEFINITION,
            "definition_revision": f"sha256:{'b' * 64}",
            "topology_class": "dynamic",
            "node_count": 13,
        },
        "revision": accepted - 1,
        "state": "SUCCEEDED",
        "total_nodes": expected_nodes,
        "completed_nodes": expected_nodes,
        "failed_nodes": 0,
        "running_nodes": 0,
        "pending_nodes": 0,
        "progress_percent": 100.0,
        "started_at": 1_785_365_100.0,
        "updated_at": 1_785_365_101.0,
        "graph": {
            "nodes": [
                {
                    "node_id": f"node-{index}",
                    "kind": "task",
                    "label": f"node {index}",
                    "callable_path": "testproject.apps.cluster_tasks.workflows.run_cpu_work_item",
                    "dependencies": sorted(inbound[f"node-{index}"]),
                    "runtime_env": {"mode": "inherit"},
                    "ray_options": {},
                    "state": "SUCCEEDED",
                    "progress": None,
                    "execution": {},
                    "started_at": 1_785_365_100.0,
                    "finished_at": 1_785_365_101.0,
                    "error": None,
                }
                for index in range(expected_nodes)
            ],
            "edges": edges,
        },
        "recent_events": [],
        "ingress": {
            "accepted": accepted,
            "rejected": 0,
            "truncated": 0,
            "accepted_by_kind": accepted_by_kind,
            "rejected_by_reason": dict.fromkeys(
                benchmark._EXPECTED_REJECTION_REASONS,
                0,
            ),
            "retained_bytes": 0,
            "retained_nodes": expected_nodes,
            "retained_edges": expected_edges,
        },
    }
    _refresh_retained_bytes(snapshot)
    return json.dumps(snapshot)


def _execution(policy: benchmark.Policy) -> RayTaskExecution:
    created_at = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    started_at = created_at + timedelta(seconds=0.1)
    finished_at = started_at + timedelta(seconds=0.2)
    plan_json, fingerprint = _plan()
    execution = RayTaskExecution.objects.create(
        task_id=f"workflow-reporting-benchmark-{policy}-{uuid4()}",
        callable_path=benchmark.EXPECTED_CALLABLE_PATH,
        state=TaskState.SUCCEEDED,
        attempt_number=1,
        execution_generation=1,
        created_at=created_at,
        started_at=started_at,
        finished_at=finished_at,
        args_json="[]",
        kwargs_json="{}",
        result_data=_result_data(),
        progress_data=None,
        workflow_run_id=uuid4(),
        workflow_plan_fingerprint=fingerprint,
        workflow_plan_pinned_attempt=1,
        workflow_plan_json=plan_json,
        workflow_plan_selection=_selection(policy),
    )
    if policy == "full":
        execution.progress_data = _ingress_progress_for(execution)
        execution.save(update_fields=["progress_data"])
    if policy == "terminal_only":
        summary = terminal_only_workflow_progress_summary(
            execution,
            declared_node_count=9,
            declared_edge_count=10,
        )
        summary["selected_strategy"] = "dynamic_tasks"
        summary["plan_fingerprint"] = fingerprint
        identity = WorkflowRunIdentity(
            task_execution_pk=execution.pk,
            attempt_number=execution.attempt_number,
            execution_generation=execution.execution_generation,
            run_id=str(execution.workflow_run_id),
        )
        execution.workflow_progress_summary_json = serialize_workflow_progress_summary(
            summary,
            expected_identity=identity,
        )
        execution.save(update_fields=["workflow_progress_summary_json"])
    TaskAttempt.objects.create(
        execution=execution,
        attempt_number=1,
        state=TaskState.SUCCEEDED,
        started_at=started_at,
        finished_at=finished_at,
        result_data=execution.result_data,
        workflow_progress_summary_json=execution.workflow_progress_summary_json,
    )
    return execution


def _cleanup_report(pk: int = 41) -> dict[str, object]:
    return {
        "benchmark": benchmark.BENCHMARK_ID,
        "schema_version": benchmark.BENCHMARK_SCHEMA_VERSION,
        "samples": [{"execution": {"pk": pk}}],
        "cleanup": {
            "requested": True,
            "status": "pending",
            "execution_rows_deleted": 0,
            "retained_for_admin_inspection": True,
        },
    }


def test_policy_orders_are_counterbalanced_and_repeat() -> None:
    assert [benchmark._policy_order(index) for index in range(6)] == [
        benchmark.POLICY_ORDERS[0],
        benchmark.POLICY_ORDERS[1],
        benchmark.POLICY_ORDERS[2],
        benchmark.POLICY_ORDERS[0],
        benchmark.POLICY_ORDERS[1],
        benchmark.POLICY_ORDERS[2],
    ]
    for position in range(3):
        assert {order[position] for order in benchmark.POLICY_ORDERS} == set(benchmark.POLICIES)
    with pytest.raises(ValueError, match="non-negative integer"):
        benchmark._policy_order(-1)
    with pytest.raises(ValueError, match="non-negative integer"):
        benchmark._policy_order(True)


def test_nearest_rank_distribution_is_explicit() -> None:
    assert benchmark._nearest_rank([1.0, 2.0, 3.0], 0.95) == 3.0
    assert benchmark._distribution([1, 2, 3]) == {
        "samples": 3,
        "median": 2.0,
        "p95_nearest_rank": 3.0,
        "minimum": 1.0,
        "maximum": 3.0,
    }
    with pytest.raises(ValueError, match="at least one"):
        benchmark._nearest_rank([], 0.95)
    with pytest.raises(ValueError, match="between zero and one"):
        benchmark._nearest_rank([1.0], 0.0)


def test_workload_and_measurement_coverage_are_bounded() -> None:
    first = benchmark._workload(
        fast_items=2,
        slow_items=1,
        fast_seconds=0.01,
        slow_seconds=0.02,
    )
    second = benchmark._workload(
        fast_items=2,
        slow_items=1,
        fast_seconds=0.01,
        slow_seconds=0.02,
    )
    changed = benchmark._workload(
        fast_items=3,
        slow_items=1,
        fast_seconds=0.01,
        slow_seconds=0.02,
    )
    coverage = benchmark._measurement_coverage()

    assert first == second
    assert first["fingerprint"] != changed["fingerprint"]
    assert benchmark._expected_dynamic_topology(fast_items=2, slow_items=1) == (9, 10)
    assert coverage["durable_task_timing"]["status"] == "measured"
    assert coverage["actor_creation_count"]["status"] == "derived"
    assert coverage["mailbox_depth_and_lag"]["status"] == "unavailable"
    assert coverage["database_statements_latency_and_wal"]["status"] == "unavailable"


def test_command_requires_explicit_live_opt_in(monkeypatch) -> None:
    monkeypatch.delenv(benchmark.OPT_IN_ENV, raising=False)

    with pytest.raises(CommandError, match=benchmark.OPT_IN_ENV):
        call_command("django_ray_benchmark_workflow_reporting")


def test_command_emits_json_and_forwards_bounded_options(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(benchmark.OPT_IN_ENV, "1")
    captured: dict[str, object] = {}

    def run_benchmark(**options: object) -> dict[str, object]:
        captured.update(options)
        return {"benchmark": benchmark.BENCHMARK_ID, "schema_version": 1}

    monkeypatch.setattr(benchmark, "_run_benchmark", run_benchmark)
    stdout = StringIO()
    stderr = StringIO()
    output = tmp_path / "report.json"

    call_command(
        "django_ray_benchmark_workflow_reporting",
        repetitions=6,
        fast_items=3,
        slow_items=2,
        fast_seconds=0.02,
        slow_seconds=0.03,
        timeout_seconds=60,
        poll_interval_seconds=0.1,
        cleanup=False,
        output_json=output,
        stdout=stdout,
        stderr=stderr,
    )

    expected = {"benchmark": benchmark.BENCHMARK_ID, "schema_version": 1}
    assert json.loads(stdout.getvalue()) == expected
    assert json.loads(output.read_text(encoding="utf-8")) == expected
    assert captured["repetitions"] == 6
    assert captured["fast_items"] == 3
    assert captured["slow_items"] == 2
    assert captured["cleanup"] is False
    assert callable(captured["progress"])


def test_command_refuses_existing_output_before_running(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(benchmark.OPT_IN_ENV, "yes")
    output = tmp_path / "existing.json"
    output.write_text("owned", encoding="utf-8")
    monkeypatch.setattr(
        benchmark,
        "_run_benchmark",
        lambda **_options: pytest.fail("live benchmark should not start"),
    )

    with pytest.raises(CommandError, match="refusing to overwrite"):
        call_command(
            "django_ray_benchmark_workflow_reporting",
            output_json=output,
        )


def test_command_requires_complete_counterbalanced_repetitions(monkeypatch) -> None:
    monkeypatch.setenv(benchmark.OPT_IN_ENV, "1")
    monkeypatch.setattr(
        benchmark,
        "_run_benchmark",
        lambda **_options: pytest.fail("invalid matrix should not start"),
    )

    with pytest.raises(CommandError, match="multiple of 3"):
        call_command(
            "django_ray_benchmark_workflow_reporting",
            repetitions=4,
        )


def test_command_requires_artifact_before_cleanup(monkeypatch) -> None:
    monkeypatch.setenv(benchmark.OPT_IN_ENV, "1")
    monkeypatch.setattr(
        benchmark,
        "_run_benchmark",
        lambda **_options: pytest.fail("unsafe cleanup should not start"),
    )

    with pytest.raises(CommandError, match="requires --output-json"):
        call_command(
            "django_ray_benchmark_workflow_reporting",
            cleanup=True,
        )


def test_cleanup_writes_evidence_before_deletion(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(benchmark.OPT_IN_ENV, "1")
    output = tmp_path / "cleanup-report.json"
    report = _cleanup_report()
    observed: dict[str, object] = {}

    monkeypatch.setattr(benchmark, "_run_benchmark", lambda **_options: report)

    def cleanup(evidence: object) -> int:
        observed["report"] = evidence
        observed["artifact_before_cleanup"] = json.loads(output.read_text(encoding="utf-8"))
        return 1

    monkeypatch.setattr(benchmark, "_cleanup_owned_executions", cleanup)
    stdout = StringIO()

    call_command(
        "django_ray_benchmark_workflow_reporting",
        cleanup=True,
        output_json=output,
        stdout=stdout,
    )

    preliminary = cast(dict[str, Any], observed["artifact_before_cleanup"])
    assert preliminary["cleanup"]["status"] == "pending"
    final = json.loads(output.read_text(encoding="utf-8"))
    assert final["cleanup"] == {
        "execution_rows_deleted": 1,
        "requested": True,
        "retained_for_admin_inspection": False,
        "status": "completed",
    }
    assert json.loads(stdout.getvalue()) == final


def test_output_failure_preserves_rows_before_cleanup(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(benchmark.OPT_IN_ENV, "1")
    report = _cleanup_report()
    cleanup_called = False
    monkeypatch.setattr(benchmark, "_run_benchmark", lambda **_options: report)

    def fail_write(_path: Path, _serialized: str) -> None:
        raise OSError("artifact unavailable")

    def cleanup(_evidence: object) -> int:
        nonlocal cleanup_called
        cleanup_called = True
        return 1

    monkeypatch.setattr(benchmark, "_write_new_report", fail_write)
    monkeypatch.setattr(benchmark, "_cleanup_owned_executions", cleanup)

    with pytest.raises(CommandError, match="artifact unavailable"):
        call_command(
            "django_ray_benchmark_workflow_reporting",
            cleanup=True,
            output_json=tmp_path / "report.json",
        )

    assert cleanup_called is False


@pytest.mark.parametrize(
    ("method", "value", "message"),
    [
        ("_bounded_int", True, "between"),
        ("_bounded_int", 2, "between"),
        ("_bounded_float", float("nan"), "between"),
        ("_bounded_float", 0.0, "between"),
    ],
)
def test_command_validators_reject_invalid_values(method, value, message) -> None:
    kwargs: dict[str, int | float] = {"minimum": 3, "maximum": 30}
    if method == "_bounded_float":
        kwargs = {"minimum": 0.01, "maximum": 10.0}
    with pytest.raises(CommandError, match=message):
        getattr(benchmark.Command, method)(value, "--value", **kwargs)


def test_wait_for_terminal_uses_bounded_polling() -> None:
    states = iter(
        [
            SimpleNamespace(state=TaskState.QUEUED),
            SimpleNamespace(state=TaskState.SUCCEEDED),
        ]
    )
    ticks = iter([0.0, 0.1, 0.2])
    sleeps: list[float] = []

    execution, elapsed, polls = benchmark._wait_for_terminal(
        12,
        timeout_seconds=1.0,
        poll_interval_seconds=0.25,
        load_execution=lambda _pk: cast(Any, next(states)),
        monotonic=lambda: next(ticks),
        sleep=sleeps.append,
    )

    assert execution.state == TaskState.SUCCEEDED
    assert elapsed == pytest.approx(0.2)
    assert polls == 2
    assert sleeps == [0.25]


def test_wait_for_terminal_fails_at_deadline() -> None:
    ticks = iter([0.0, 1.0])

    with pytest.raises(
        benchmark.WorkflowReportingBenchmarkError,
        match="did not become terminal",
    ):
        benchmark._wait_for_terminal(
            13,
            timeout_seconds=1.0,
            poll_interval_seconds=0.25,
            load_execution=lambda _pk: cast(
                Any,
                SimpleNamespace(state=TaskState.RUNNING),
            ),
            monotonic=lambda: next(ticks),
            sleep=lambda _seconds: pytest.fail("deadline should not sleep"),
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("policy", "pilot_enabled", "availability"),
    [
        ("full", False, None),
        ("terminal_only", True, "OMITTED_BY_POLICY"),
        ("disabled", True, None),
    ],
)
def test_sample_validates_actor_free_and_package_default_contracts(
    policy: benchmark.Policy,
    pilot_enabled: bool,
    availability: str | None,
) -> None:
    execution = _execution(policy)

    sample = benchmark._sample(
        execution,
        cycle=1,
        position=1,
        policy=policy,
        client_poll_seconds=0.3,
        poll_count=2,
        fast_items=2,
        slow_items=1,
        pilot_enabled=pilot_enabled,
    )

    assert sample["policy"] == policy
    assert sample["timing"] == {
        "queue_wait_seconds": 0.1,
        "outer_execution_seconds": 0.2,
        "durable_end_to_end_seconds": 0.3,
    }
    reporting = cast(dict[str, Any], sample["reporting"])
    assert reporting["actor_expected_count"] == int(policy == "full")
    assert (reporting["ingress"] is not None) is (policy == "full")
    selection = cast(dict[str, Any], sample["selection"])
    assert selection["plan_node_count"] == 13
    assert selection["plan_edge_count"] == 13
    summary = cast(dict[str, Any] | None, sample["summary"])
    assert (summary or {}).get("detail_availability") == availability
    durable_storage = cast(dict[str, Any], sample["durable_reporting_storage"])
    shared_storage = cast(dict[str, Any], sample["shared_lifecycle_storage"])
    assert "attempt_summary_bytes" not in shared_storage
    assert durable_storage["attempt_summary_bytes"] == durable_storage["summary_bytes"]
    serialized = json.dumps(sample)
    assert "private_application_data" not in serialized
    assert "not emitted" not in serialized


@pytest.mark.django_db
def test_sample_rejects_invalid_plan_and_selection_evidence() -> None:
    execution = _execution("disabled")
    execution.workflow_plan_json = "{}"
    execution.save(update_fields=["workflow_plan_json"])

    with pytest.raises(
        benchmark.WorkflowReportingBenchmarkError,
        match="invalid workflow plan",
    ):
        benchmark._sample(
            execution,
            cycle=1,
            position=1,
            policy="disabled",
            client_poll_seconds=0.1,
            poll_count=1,
            fast_items=2,
            slow_items=1,
            pilot_enabled=True,
        )

    plan_json, fingerprint = _plan()
    execution.workflow_plan_json = plan_json
    execution.workflow_plan_fingerprint = fingerprint
    execution.workflow_plan_selection = json.dumps(
        {
            "plan_selection_format": "django-ray.workflow-plan-selection",
            "plan_selection_format_version": 2,
            "reporting_policy": "disabled",
            "selected_strategy": "dynamic_tasks",
        }
    )
    execution.save(
        update_fields=[
            "workflow_plan_json",
            "workflow_plan_fingerprint",
            "workflow_plan_selection",
        ]
    )

    with pytest.raises(
        benchmark.WorkflowReportingBenchmarkError,
        match="invalid workflow plan",
    ):
        benchmark._sample(
            execution,
            cycle=1,
            position=1,
            policy="disabled",
            client_poll_seconds=0.1,
            poll_count=1,
            fast_items=2,
            slow_items=1,
            pilot_enabled=True,
        )


@pytest.mark.django_db
@pytest.mark.parametrize("corruption", ["identity", "truncated", "topology"])
def test_full_sample_rejects_incomplete_actor_evidence(corruption: str) -> None:
    execution = _execution("full")
    snapshot = json.loads(cast(str, execution.progress_data))
    if corruption == "identity":
        snapshot["run_identity"]["run_id"] = str(uuid4())
    elif corruption == "truncated":
        snapshot["ingress"]["truncated"] = 1
    else:
        snapshot["graph"]["nodes"].pop()
    execution.progress_data = json.dumps(snapshot)
    execution.save(update_fields=["progress_data"])

    with pytest.raises(
        benchmark.WorkflowReportingBenchmarkError,
        match="invalid or incomplete|expanded workload",
    ):
        benchmark._sample(
            execution,
            cycle=1,
            position=1,
            policy="full",
            client_poll_seconds=0.1,
            poll_count=1,
            fast_items=2,
            slow_items=1,
            pilot_enabled=False,
        )


@pytest.mark.django_db
def test_sample_rejects_terminal_failure() -> None:
    execution = _execution("disabled")
    execution.state = TaskState.FAILED
    execution.save(update_fields=["state"])

    with pytest.raises(
        benchmark.WorkflowReportingBenchmarkError,
        match="did not succeed",
    ):
        benchmark._sample(
            execution,
            cycle=1,
            position=1,
            policy="disabled",
            client_poll_seconds=0.1,
            poll_count=1,
            fast_items=2,
            slow_items=1,
            pilot_enabled=True,
        )


@pytest.mark.django_db
def test_cleanup_deletes_only_exact_owned_callable_rows() -> None:
    owned = _execution("disabled")
    unrelated = RayTaskExecution.objects.create(
        task_id=f"unrelated-cleanup-{uuid4()}",
        callable_path="testproject.tasks.add",
        state=TaskState.SUCCEEDED,
    )
    report = _cleanup_report(owned.pk)

    assert benchmark._cleanup_owned_executions(report) == 1
    assert not RayTaskExecution.objects.filter(pk=owned.pk).exists()
    assert RayTaskExecution.objects.filter(pk=unrelated.pk).exists()

    wrong_report = _cleanup_report(unrelated.pk)
    with pytest.raises(
        benchmark.WorkflowReportingBenchmarkError,
        match="could not prove exact execution ownership",
    ):
        benchmark._cleanup_owned_executions(wrong_report)
    assert RayTaskExecution.objects.filter(pk=unrelated.pk).exists()


@pytest.mark.django_db
def test_storage_counts_logical_aggregates_without_double_counting() -> None:
    execution = _execution("disabled")
    identity = benchmark._run_identity(execution)
    detail_payload = b'{"node_id":"node-a","state":"SUCCEEDED"}'
    page_payload = b'{"items":[{"node_id":"node-a"}]}'
    manifest_payload = b'{"pages":[]}'
    run = WorkflowProgressRunStorage.objects.create(
        execution=execution,
        attempt_number=identity.attempt_number,
        execution_generation=identity.execution_generation,
        run_id=identity.run_id,
        detail_revision=1,
        detail_node_count=1,
        detail_succeeded_count=1,
        detail_event_count=1,
        detail_encoded_bytes=len(detail_payload),
        detail_decoded_bytes=len(detail_payload),
    )
    page = WorkflowProgressTopologyPage.objects.create(
        run_storage=run,
        digest=hashlib.sha256(page_payload).hexdigest(),
        collection=WorkflowProgressTopologyCollection.NODE,
        payload=page_payload,
        item_count=1,
        encoded_bytes=len(page_payload),
        decoded_bytes=len(page_payload),
    )
    manifest = WorkflowProgressTopologyManifest.objects.create(
        run_storage=run,
        topology_version=1,
        slot=WorkflowProgressTopologySlot.CURRENT,
        manifest_digest="a" * 64,
        payload=manifest_payload,
        node_count=1,
        edge_count=0,
        node_page_count=1,
        edge_page_count=0,
        encoded_bytes=len(manifest_payload) + len(page_payload),
        decoded_bytes=len(manifest_payload) + len(page_payload),
        published_at=timezone.now(),
    )
    WorkflowProgressTopologyManifestPage.objects.create(
        manifest=manifest,
        page=page,
        collection=WorkflowProgressTopologyCollection.NODE,
        page_index=0,
    )
    WorkflowProgressNodeDetail.objects.create(
        run_storage=run,
        node_key=hashlib.sha256(b"node-a").hexdigest(),
        node_id="node-a",
        state=WorkflowProgressNodeState.SUCCEEDED,
        event_count=1,
        payload=detail_payload,
        digest=hashlib.sha256(detail_payload).hexdigest(),
        encoded_bytes=len(detail_payload),
        decoded_bytes=len(detail_payload),
        last_topology_version=1,
        last_detail_revision=1,
    )

    storage = benchmark._storage(execution, identity=identity)

    assert storage["run_storage"] == {
        "rows": 1,
        "detail_encoded_bytes": len(detail_payload),
        "detail_decoded_bytes": len(detail_payload),
    }
    manifests = cast(dict[str, Any], storage["topology_manifests"])
    pages = cast(dict[str, Any], storage["topology_pages"])
    assert manifests["topology_encoded_bytes"] == len(manifest_payload) + len(page_payload)
    assert pages["encoded_bytes"] == len(page_payload)
    assert pages["unlinked_rows"] == 0
    assert storage["manifest_links"] == {"rows": 1}
    assert cast(dict[str, Any], storage["node_details"])["encoded_bytes"] == len(detail_payload)


def test_complete_report_rejects_incomplete_or_drifted_matrix() -> None:
    samples: list[dict[str, object]] = []
    execution_pk = 1
    for cycle in range(3):
        for position, policy in enumerate(benchmark._policy_order(cycle), start=1):
            samples.append(
                {
                    "cycle": cycle + 1,
                    "position": position,
                    "policy": policy,
                    "selection": {"plan_fingerprint": f"sha256:{'a' * 64}"},
                    "execution": {"pk": execution_pk},
                }
            )
            execution_pk += 1

    assert benchmark._validate_complete_report(samples, repetitions=3) == (f"sha256:{'a' * 64}")

    with pytest.raises(
        benchmark.WorkflowReportingBenchmarkError,
        match="incomplete",
    ):
        benchmark._validate_complete_report(samples[:-1], repetitions=3)

    drifted = [dict(sample) for sample in samples]
    drifted[-1]["selection"] = {"plan_fingerprint": f"sha256:{'b' * 64}"}
    with pytest.raises(
        benchmark.WorkflowReportingBenchmarkError,
        match="drifted",
    ):
        benchmark._validate_complete_report(drifted, repetitions=3)


def test_policy_aggregates_never_rank_or_claim_causality() -> None:
    samples: list[dict[str, object]] = []
    for index, policy in enumerate(benchmark.POLICIES, start=1):
        for value in (float(index), float(index + 1), float(index + 2)):
            samples.append(
                {
                    "policy": policy,
                    "timing": {
                        "outer_execution_seconds": value,
                        "durable_end_to_end_seconds": value + 0.1,
                    },
                    "workload_result": {
                        "workflow_elapsed_seconds": value - 0.1,
                        "useful_leaf_seconds": 0.04,
                    },
                }
            )

    aggregates = benchmark._policy_aggregates(samples)

    assert set(aggregates) == set(benchmark.POLICIES)
    assert cast(dict[str, Any], aggregates["full"])["sample_count"] == 3
    assert "winner" not in json.dumps(aggregates)
    assert "speedup" not in json.dumps(aggregates)
