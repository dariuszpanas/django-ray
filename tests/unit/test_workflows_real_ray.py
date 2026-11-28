"""Local-Ray integration tests for workflow execution and schema v3 progress."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from django_ray.runtime.context import (
    WorkflowRunIdentity,
    durable_task_execution,
)
from django_ray.workflow.progress.protocol import (
    WorkflowProgressEventKind,
)
from django_ray.workflows import (
    chain,
    group,
    map_step,
    step,
)
from tests.local_ray import local_ray_runtime
from tests.unit._workflows_helpers import (
    DelayedFirstSnapshotProgressActor,
    _ConcurrencyTracker,
    _LifecycleTracker,
    _strict_workflow_fixture_identity,
)
from tests.unit.test_workflows import (
    fail_chain_terminal,
    fail_group_branch,
    fail_workflow_step,
    finish_or_cancel_group_branch,
    identity,
    increment,
    multiply,
    record_chain_upstream,
    report_and_increment,
    report_then_make_range,
    run_nested_workflow,
    track_barrier_value,
)


@pytest.fixture(scope="module")
def ray_cluster():
    with local_ray_runtime(num_cpus=4) as ray:
        yield ray


def _run_nested_workflow_with_test_allowlist(limit: int) -> int:
    """Run the nested workflow inside a Ray worker with explicit test module allowlist."""
    from django.conf import settings as django_settings

    if not django_settings.configured:
        django_settings.configure(DJANGO_RAY={"ALLOWED_CALLABLE_MODULES": ["tests"]})
        return run_nested_workflow(limit)

    previous = getattr(django_settings, "DJANGO_RAY", None)
    current = dict(previous) if isinstance(previous, dict) else {}
    configured = list(current.get("ALLOWED_CALLABLE_MODULES") or ())
    if "tests" not in configured:
        configured.append("tests")
    current["ALLOWED_CALLABLE_MODULES"] = configured
    django_settings.DJANGO_RAY = current
    try:
        return run_nested_workflow(limit)
    finally:
        if previous is None:
            delattr(django_settings, "DJANGO_RAY")
        else:
            django_settings.DJANGO_RAY = previous


@pytest.mark.real_ray
def test_workflow_executes_on_real_ray(ray_cluster: Any) -> None:
    outer_task = ray_cluster.remote(max_calls=1)(_run_nested_workflow_with_test_allowlist)
    assert ray_cluster.get(outer_task.remote(5)) == 40


@pytest.mark.django_db
@pytest.mark.real_ray
def test_real_ray_actor_free_reporting_policies_create_no_actor_evidence(
    monkeypatch,
    settings,
    ray_cluster: Any,
) -> None:
    del ray_cluster

    import django_ray.workflows as workflow_module
    from django_ray.models import RayTaskExecution, TaskState, WorkflowProgressRunStorage

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
    }

    cases = [
        (policy, requested_strategy)
        for policy in ("terminal_only", "disabled")
        for requested_strategy in (None, "auto", "dynamic_tasks")
    ]
    executions = {
        (policy, requested_strategy): RayTaskExecution.objects.create(
            task_id=f"real-ray-{policy}-{requested_strategy or 'per-call'}-progress",
            callable_path=f"{report_and_increment.__module__}.report_and_increment",
            state=TaskState.RUNNING,
            execution_generation=1,
        )
        for policy, requested_strategy in cases
    }
    actor_creation_attempts: list[str] = []

    class ForbiddenProgressActor:
        @staticmethod
        def remote(*args, **kwargs):
            del args, kwargs
            actor_creation_attempts.append("created")
            raise AssertionError("actor-free reporting policy created a progress actor")

    remote_step, remote_collect, _ = workflow_module._get_cached_workflow_remotes()
    monkeypatch.setattr(
        workflow_module,
        "_get_cached_workflow_remotes",
        lambda: (remote_step, remote_collect, ForbiddenProgressActor),
    )
    results = {}
    for (policy, requested_strategy), execution in executions.items():
        settings.DJANGO_RAY = {
            **settings.DJANGO_RAY,
            "WORKFLOW_PROGRESS_REPORTING_POLICY": (
                policy if requested_strategy is not None else "full"
            ),
        }
        workflow = step(report_and_increment)
        configured = (
            workflow.with_progress_reporting(policy)
            if requested_strategy is None
            else workflow.with_execution_strategy(requested_strategy)
        )
        with durable_task_execution(
            execution.pk,
            attempt_number=execution.attempt_number,
            execution_generation=execution.execution_generation,
            **_strict_workflow_fixture_identity(execution),
        ):
            results[policy, requested_strategy] = configured.run(5, use_ray=True)

    assert actor_creation_attempts == []
    assert results == dict.fromkeys(cases, (False, 6))
    for (policy, requested_strategy), execution in executions.items():
        execution.refresh_from_db()
        selection = json.loads(execution.workflow_plan_selection)
        assert selection["requested_policy"] == (requested_strategy or "dynamic_tasks")
        assert selection["selected_strategy"] == "dynamic_tasks"
        assert selection["reporting_policy"] == policy
        assert execution.progress_data is None
        assert execution.workflow_progress_summary_json is None
        assert not WorkflowProgressRunStorage.objects.filter(execution=execution).exists()


@pytest.mark.real_ray
def test_real_ray_bounded_map_preserves_order_and_limits_concurrency(ray_cluster: Any) -> None:
    tracker = ray_cluster.remote(num_cpus=0)(_ConcurrencyTracker).remote()
    try:
        workflow = chain(
            step(identity),
            map_step(
                track_barrier_value,
                tracker,
                ray_options={"num_cpus": 0},
            ).with_limits(max_concurrency=2, max_items=10),
        )
        items = [0, 1, 2, 3]

        assert workflow.run(items, use_ray=True) == [0, 1, 2, 3]
        assert ray_cluster.get(tracker.snapshot.remote()) == (0, 2)
    finally:
        ray_cluster.kill(tracker, no_restart=True)


@pytest.mark.real_ray
def test_real_ray_mapped_group_cleans_sibling_ref_before_failure_returns(ray_cluster: Any) -> None:
    from ray.exceptions import RayTaskError

    tracker = ray_cluster.remote(num_cpus=0)(_LifecycleTracker).remote()
    try:
        cancel_timeout_seconds = 1.5
        workflow = chain(
            step(identity),
            map_step(
                group(
                    step(
                        fail_group_branch,
                        tracker,
                        ray_options={"num_cpus": 0},
                    ),
                    step(
                        finish_or_cancel_group_branch,
                        tracker,
                        ray_options={"num_cpus": 0},
                    ),
                )
            ).with_limits(
                max_concurrency=1,
                max_items=1,
                cancel_timeout_seconds=cancel_timeout_seconds,
            ),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            workflow_result = pool.submit(workflow.run, [1], use_ray=True)
            required_events = {"failing_started", "sibling_started"}
            deadline = time.monotonic() + 15.0
            while True:
                events = ray_cluster.get(tracker.snapshot.remote())
                if required_events <= set(events):
                    break
                if workflow_result.done():
                    unexpected_result = workflow_result.result()
                    pytest.fail(f"mapped group unexpectedly returned {unexpected_result!r}")
                if time.monotonic() >= deadline:
                    pytest.fail("mapped group branches did not reach the start barrier")
                time.sleep(0.01)

            started_at = time.monotonic()
            ray_cluster.get(tracker.record.remote("release_failure"))
            with pytest.raises(RayTaskError, match="original mapped group failure"):
                workflow_result.result(timeout=5.0)
            elapsed = time.monotonic() - started_at

        events = ray_cluster.get(tracker.snapshot.remote())
        assert "failing_started" in events
        assert "sibling_started" in events
        assert {"sibling_cancelled", "sibling_completed"} & set(events)
        assert elapsed < cancel_timeout_seconds + 1.0
    finally:
        ray_cluster.kill(tracker, no_restart=True)


@pytest.mark.real_ray
def test_real_ray_mapped_chain_drains_upstream_ref_and_preserves_failure(ray_cluster: Any) -> None:
    from ray.exceptions import RayTaskError

    tracker = ray_cluster.remote(num_cpus=0)(_LifecycleTracker).remote()
    try:
        workflow = chain(
            step(identity),
            map_step(
                chain(
                    step(
                        record_chain_upstream,
                        tracker,
                        ray_options={"num_cpus": 0},
                    ),
                    step(
                        fail_chain_terminal,
                        tracker,
                        ray_options={"num_cpus": 0},
                    ),
                )
            ).with_limits(
                max_concurrency=1,
                max_items=1,
                cancel_timeout_seconds=1.0,
            ),
        )

        with pytest.raises(RayTaskError, match="original mapped chain failure"):
            workflow.run([1], use_ray=True)

        events = ray_cluster.get(tracker.snapshot.remote())
        assert events == ["upstream_completed", "terminal_started"]
    finally:
        ray_cluster.kill(tracker, no_restart=True)


@pytest.mark.real_ray
@pytest.mark.django_db
def test_real_ray_workflow_persists_graph_after_delayed_progress_actor_snapshot(
    monkeypatch,
    settings,
    ray_cluster: Any,
) -> None:
    import django_ray.workflows as workflow_module
    from django_ray.models import RayTaskExecution, WorkflowProgressRunStorage

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "WORKFLOW_PROGRESS_FLUSH_SECONDS": 300,
    }
    execution = RayTaskExecution.objects.create(
        task_id="real-ray-workflow-graph",
        callable_path="tests.unit.test_workflows.run_nested_workflow",
        state="RUNNING",
        execution_generation=1,
    )
    workflow = chain(
        step(increment),
        step(multiply, factor=2),
    )

    remote_step, remote_collect, _ = workflow_module._get_cached_workflow_remotes()
    delayed_progress_actor = ray_cluster.remote(num_cpus=0)(DelayedFirstSnapshotProgressActor)
    monkeypatch.setattr(
        workflow_module,
        "_get_cached_workflow_remotes",
        lambda: (remote_step, remote_collect, delayed_progress_actor),
    )
    with durable_task_execution(
        execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
        runtime_env_profile="test",
        runtime_env_hash="abc123",
        **_strict_workflow_fixture_identity(execution, profile="test"),
    ):
        assert workflow.run(2, use_ray=True) == 6

    from django_ray.workflow.progress.reads import (
        get_workflow_node_detail,
        get_workflow_progress_summary,
        list_workflow_topology_edges,
        list_workflow_topology_nodes,
    )

    execution.refresh_from_db()
    assert execution.progress_data is None

    def authorize(candidate):
        return candidate.pk == execution.pk

    summary = get_workflow_progress_summary(execution, authorize=authorize)
    edges = list_workflow_topology_edges(execution, authorize=authorize)
    node = get_workflow_node_detail(execution, "0.0", authorize=authorize)["item"]
    assert summary["source_schema_version"] == 3
    assert summary["availability"] == "AVAILABLE"
    stored_summary = json.loads(execution.workflow_progress_summary_json)
    assert stored_summary["state"] == "SUCCEEDED"
    assert stored_summary["run_identity"]["attempt_number"] == 1
    assert stored_summary["run_identity"]["execution_generation"] == 1
    assert edges["items"] == [{"source": "0.0", "target": "0.1"}]
    assert node["state"] == "SUCCEEDED"
    topology = list_workflow_topology_nodes(execution, authorize=authorize)
    declaration = next(item for item in topology["items"] if item["node_id"] == "0.0")
    assert declaration["runtime_env"]["mode"] == "inherit"
    assert declaration["runtime_env"]["profile"] == "test"
    assert declaration["runtime_env"]["hash"].startswith("sha256:")
    assert node["execution"]["ray_task_id"]
    assert node["execution"]["ray_node_id"]
    assert WorkflowProgressRunStorage.objects.filter(execution=execution).count() == 1


@pytest.mark.real_ray
@pytest.mark.django_db
def test_real_ray_cached_actor_publishes_schema_v3_through_production_path(
    settings,
    ray_cluster: Any,
) -> None:
    del ray_cluster

    import django_ray.workflows as workflow_module
    from django_ray.models import (
        RayTaskExecution,
        WorkflowProgressNodeDetail,
        WorkflowProgressRunStorage,
        WorkflowProgressTopologyManifest,
        WorkflowProgressTopologyPage,
        WorkflowProgressTopologySlot,
    )
    from django_ray.workflow.progress.publication import (
        WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS,
    )
    from django_ray.workflow.progress.reads import (
        get_workflow_node_detail,
        get_workflow_progress_summary,
        list_workflow_topology_edges,
        list_workflow_topology_nodes,
    )
    from django_ray.workflow.progress.reporting_diagnostics import load_reporting_diagnostics
    from django_ray.workflow.progress.summary import deserialize_workflow_progress_summary

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
    }

    execution = RayTaskExecution.objects.create(
        task_id="real-ray-bounded-map-graph",
        callable_path="tests.unit.test_workflows.run_nested_workflow",
        state="RUNNING",
        execution_generation=1,
    )
    workflow = chain(
        step(report_then_make_range),
        map_step(increment).with_limits(max_concurrency=2, max_items=10),
    )

    _, _, cached_progress_actor = workflow_module._get_cached_workflow_remotes()
    assert cached_progress_actor is workflow_module._workflow_progress_actor_cached
    with durable_task_execution(
        execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
        **_strict_workflow_fixture_identity(execution),
    ):
        assert workflow.run(6, use_ray=True) == [1, 2, 3, 4, 5, 6]

    execution.refresh_from_db()
    assert execution.progress_data is None
    summary = deserialize_workflow_progress_summary(execution.workflow_progress_summary_json)
    identity = WorkflowRunIdentity(
        task_execution_pk=execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
        run_id=str(execution.workflow_run_id),
    )
    diagnostics = load_reporting_diagnostics(identity, detail_revision=summary["detail_revision"])
    ingress = diagnostics["ingress"]

    def authorize(candidate):
        return candidate.pk == execution.pk

    root_node = get_workflow_node_detail(execution, "0.0", authorize=authorize)["item"]
    map_node = get_workflow_node_detail(execution, "0.1", authorize=authorize)["item"]
    assert summary["state"] == "SUCCEEDED"
    assert summary["run_identity"] == identity.as_dict()
    assert diagnostics["snapshot_revision"] > 0
    assert root_node["progress"]["message"] == "Preparing bounded fan-out"
    assert root_node["progress"]["metrics"] == {"items": 6}
    assert map_node["node_id"] == "0.1"
    topology = list_workflow_topology_nodes(execution, authorize=authorize)
    map_declaration = next(item for item in topology["items"] if item["node_id"] == "0.1")
    assert map_declaration["kind"] == "map"
    assert map_node["fanout"]["max_concurrency"] == 2
    assert map_node["fanout"]["max_items"] == 10
    assert map_node["fanout"]["submitted_items"] == 6
    assert map_node["fanout"]["completed_items"] == 6
    assert map_node["fanout"]["in_flight_items"] == 0
    assert map_node["fanout"]["input_exhausted"] is True

    expected_kinds = {
        WorkflowProgressEventKind.INITIALIZED,
        WorkflowProgressEventKind.NODE_REGISTERED,
        WorkflowProgressEventKind.EDGES_REGISTERED,
        WorkflowProgressEventKind.MAP_REGISTERED,
        WorkflowProgressEventKind.SUBMITTED,
        WorkflowProgressEventKind.NODE_SETTLED,
        WorkflowProgressEventKind.PRODUCER_REPORT,
        WorkflowProgressEventKind.MAP_PROGRESS,
        WorkflowProgressEventKind.COMPLETED,
    }
    assert set(ingress["accepted_by_kind"]) == {kind.value for kind in WorkflowProgressEventKind}
    assert all(ingress["accepted_by_kind"][kind.value] >= 1 for kind in expected_kinds)
    assert ingress["accepted_by_kind"][WorkflowProgressEventKind.FAILED.value] == 0
    assert ingress["accepted_by_kind"][WorkflowProgressEventKind.STARTED.value] == 0
    assert ingress["accepted_by_kind"][WorkflowProgressEventKind.APPLICATION_PROGRESS.value] == 0
    assert ingress["accepted"] == sum(ingress["accepted_by_kind"].values())
    assert ingress["rejected"] == 0
    assert ingress["truncated"] == 0
    assert ingress["retained_nodes"] == 2
    assert ingress["retained_edges"] == 1
    assert (
        ingress["retained_nodes"]
        <= WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS.topology_node_max_items
    )
    assert (
        ingress["retained_edges"]
        <= WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS.topology_edge_max_items
    )
    assert (
        0
        < ingress["retained_bytes"]
        <= WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS.combined_max_decoded_bytes
    )
    producer = ingress["producer"]
    assert producer["schema_version"] == 1
    assert producer["saturated"] is False
    assert producer["reports"] == 1
    assert producer["offered"] >= 1
    assert producer["submitted"] >= 1
    assert producer["locally_dropped"] == 0
    assert producer["offered"] == producer["submitted"] + producer["superseded"]
    assert producer["acknowledged"] + producer["pending_acknowledgements"] == producer["submitted"]
    assert producer["pending_acknowledgements"] <= 2
    assert producer["actor_rejected"] == 0
    assert producer["ack_failed"] == 0
    assert producer["terminal_handoffs"]["failed"] == 0
    assert producer["terminal_handoffs"]["actor_unavailable"] == 0
    assert sum(producer["terminal_handoffs"].values()) == 1
    assert (
        ingress["accepted_by_kind"][WorkflowProgressEventKind.MAP_PROGRESS.value]
        == producer["submitted"]
    )
    capture = ingress["capture"]
    assert capture["scope"] == "successful_final_invocations"
    assert (
        capture["reports"] == capture["offered"] == capture["accepted"] == capture["retained"] == 1
    )
    assert capture["rejected"] == capture["superseded"] == 0
    assert capture["saturated"] is False
    assert (
        0
        < capture["retained_bytes"]
        == capture["canonical_bytes"]
        <= WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS.event_wire_max_bytes
    )
    cost = ingress["cost"]
    assert set(cost) == {
        "schema_version",
        "saturated",
        "initialization",
        "ingest",
        "delivery_delay",
        "snapshot",
    }
    assert cost["schema_version"] == 1
    assert cost["saturated"] is False
    initialization_cost = cost["initialization"]
    assert set(initialization_cost) == {
        "wire_bytes",
        "handler_wall_ns",
        "handler_cpu_ns",
    }
    assert (
        0
        < initialization_cost["wire_bytes"]
        <= WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS.event_wire_max_bytes
    )
    assert initialization_cost["handler_wall_ns"] > 0
    # Windows can account very short process-CPU intervals as zero even though
    # the monotonic wall timer observes the constructor work.
    assert initialization_cost["handler_cpu_ns"] >= 0

    ingest_cost = cost["ingest"]
    assert set(ingest_cost) == {
        "calls_received",
        "wire_bytes_received",
        "decoded_calls",
        "post_disable_calls",
        "decoded_by_kind",
        "handler_wall_ns_total",
        "handler_wall_ns_max",
        "handler_cpu_ns_total",
        "handler_cpu_ns_max",
    }
    accepted_ingest_calls = ingress["accepted"] - 1
    assert ingest_cost["calls_received"] == accepted_ingest_calls
    assert ingest_cost["decoded_calls"] == accepted_ingest_calls
    assert ingest_cost["post_disable_calls"] == 0
    assert (
        accepted_ingest_calls
        <= ingest_cost["wire_bytes_received"]
        <= (accepted_ingest_calls * WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS.event_wire_max_bytes)
    )
    assert ingest_cost["decoded_by_kind"] == {
        kind.value: (
            0
            if kind is WorkflowProgressEventKind.INITIALIZED
            else ingress["accepted_by_kind"][kind.value]
        )
        for kind in WorkflowProgressEventKind
    }
    assert (
        0
        < ingest_cost["handler_wall_ns_max"]
        <= ingest_cost["handler_wall_ns_total"]
        <= accepted_ingest_calls * ingest_cost["handler_wall_ns_max"]
    )
    assert (
        0
        <= ingest_cost["handler_cpu_ns_max"]
        <= ingest_cost["handler_cpu_ns_total"]
        <= accepted_ingest_calls * ingest_cost["handler_cpu_ns_max"]
    )

    delivery_cost = cost["delivery_delay"]
    assert set(delivery_cost) == {
        "samples",
        "total_us",
        "max_us",
        "negative_clock_samples",
    }
    assert delivery_cost["negative_clock_samples"] == 0
    assert delivery_cost["samples"] == accepted_ingest_calls
    assert (
        0
        < delivery_cost["max_us"]
        <= delivery_cost["total_us"]
        <= delivery_cost["samples"] * delivery_cost["max_us"]
    )

    snapshot_cost = cost["snapshot"]
    assert set(snapshot_cost) == {
        "calls",
        "build_wall_ns_total",
        "build_wall_ns_max",
        "build_cpu_ns_total",
        "build_cpu_ns_max",
    }
    assert snapshot_cost["calls"] >= 1
    assert (
        0
        < snapshot_cost["build_wall_ns_max"]
        <= snapshot_cost["build_wall_ns_total"]
        <= snapshot_cost["calls"] * snapshot_cost["build_wall_ns_max"]
    )
    assert (
        0
        <= snapshot_cost["build_cpu_ns_max"]
        <= snapshot_cost["build_cpu_ns_total"]
        <= snapshot_cost["calls"] * snapshot_cost["build_cpu_ns_max"]
    )
    serialized_cost = json.dumps(cost, sort_keys=True)
    assert "Preparing bounded fan-out" not in serialized_cost
    assert "0.0" not in serialized_cost
    assert "0.1" not in serialized_cost

    assert execution.workflow_progress_summary_json is not None
    summary = deserialize_workflow_progress_summary(
        execution.workflow_progress_summary_json,
    )
    run_storage = WorkflowProgressRunStorage.objects.get(execution=execution)
    manifest = WorkflowProgressTopologyManifest.objects.get(run_storage=run_storage)
    assert manifest.slot == WorkflowProgressTopologySlot.CURRENT
    assert not WorkflowProgressTopologyManifest.objects.filter(
        run_storage=run_storage,
        slot=WorkflowProgressTopologySlot.PENDING,
    ).exists()
    assert not WorkflowProgressTopologyPage.objects.filter(
        run_storage=run_storage,
        manifest_links__isnull=True,
    ).exists()
    assert WorkflowProgressNodeDetail.objects.filter(run_storage=run_storage).count() == 2
    assert summary["state"] == "SUCCEEDED"
    assert summary["detail"]["availability"] == "AVAILABLE"
    assert summary["node_counts"] == {
        "declared": None,
        "discovered": 2,
        "retained_topology": 2,
        "retained_detail": 2,
        "pending": 0,
        "running": 0,
        "succeeded": 2,
        "failed": 0,
    }
    assert summary["edge_counts"] == {
        "declared": None,
        "discovered": 1,
        "retained_topology": 1,
    }
    assert summary["topology_version"] == manifest.topology_version == 1
    assert summary["detail_revision"] == run_storage.detail_revision == 1
    assert summary["storage"]["manifest_id"] == str(manifest.pk)

    def authorize(candidate):
        return candidate.pk == execution.pk

    public_summary = get_workflow_progress_summary(execution, authorize=authorize)
    topology_nodes = list_workflow_topology_nodes(execution, authorize=authorize)
    topology_edges = list_workflow_topology_edges(execution, authorize=authorize)
    root_detail = get_workflow_node_detail(execution, "0.0", authorize=authorize)
    map_detail = get_workflow_node_detail(execution, "0.1", authorize=authorize)
    assert public_summary["source_schema_version"] == 3
    assert public_summary["availability"] == "AVAILABLE"
    assert topology_nodes["returned_count"] == 2
    assert {node["node_id"] for node in topology_nodes["items"]} == {"0.0", "0.1"}
    assert topology_edges["items"] == [{"source": "0.0", "target": "0.1"}]
    assert root_detail["found"] is True
    assert root_detail["item"]["state"] == "SUCCEEDED"
    assert root_detail["item"]["execution"]["ray_task_id"]
    assert root_detail["item"]["execution"]["ray_node_id"]
    assert root_detail["item"]["progress"]["message"] == "Preparing bounded fan-out"
    assert root_detail["item"]["progress"]["metrics"] == {"items": 6}
    assert map_detail["found"] is True
    assert map_detail["item"]["state"] == "SUCCEEDED"
    assert map_detail["item"]["execution"] is None
    assert map_detail["item"]["fanout"] == {
        "max_concurrency": 2,
        "max_items": 10,
        "submitted_items": 6,
        "completed_items": 6,
        "in_flight_items": 0,
        "input_exhausted": True,
    }


@pytest.mark.real_ray
@pytest.mark.django_db
def test_real_ray_failed_leaf_publishes_failed_schema_v3_graph(
    settings,
    ray_cluster: Any,
) -> None:
    del ray_cluster
    from ray.exceptions import RayTaskError

    from django_ray.models import (
        RayTaskExecution,
        WorkflowProgressTopologyManifest,
        WorkflowProgressTopologySlot,
    )
    from django_ray.workflow.progress.reads import (
        get_workflow_progress_summary,
        list_workflow_node_details,
        list_workflow_topology_edges,
    )

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
    }
    execution = RayTaskExecution.objects.create(
        task_id="real-ray-failed-workflow-graph",
        callable_path="tests.unit.test_workflows.fail_workflow_step",
        state="RUNNING",
        execution_generation=1,
    )
    workflow = chain(
        step(increment),
        step(fail_workflow_step),
    )

    with durable_task_execution(
        execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
        **_strict_workflow_fixture_identity(execution),
    ):
        with pytest.raises(RayTaskError, match="intentional workflow failure"):
            workflow.run(1, use_ray=True)

    execution.refresh_from_db()

    def authorize(candidate):
        return candidate.pk == execution.pk

    summary = get_workflow_progress_summary(execution, authorize=authorize)
    edges = list_workflow_topology_edges(execution, authorize=authorize)
    details = list_workflow_node_details(execution, authorize=authorize)
    details_by_id = {item["node_id"]: item for item in details["items"]}
    assert summary["source_schema_version"] == 3
    assert summary["availability"] == "AVAILABLE"
    assert summary["summary"]["state"] == "FAILED"
    assert summary["summary"]["node_counts"]["succeeded"] == 1
    assert summary["summary"]["node_counts"]["failed"] == 1
    assert edges["items"] == [{"source": "0.0", "target": "0.1"}]
    assert details_by_id["0.0"]["state"] == "SUCCEEDED"
    assert details_by_id["0.1"]["state"] == "FAILED"
    assert "intentional workflow failure" in details_by_id["0.1"]["error"]
    manifests = WorkflowProgressTopologyManifest.objects.filter(
        run_storage__execution=execution,
    )
    assert manifests.filter(slot=WorkflowProgressTopologySlot.CURRENT).count() == 1
    assert not manifests.filter(slot=WorkflowProgressTopologySlot.PENDING).exists()
