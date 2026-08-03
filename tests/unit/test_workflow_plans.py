"""Tests for canonical effective workflow plans and durable identity pinning."""

from __future__ import annotations

import json
import pickle
import sys
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from django_ray.lifecycle import record_failure
from django_ray.models import RayTaskExecution, TaskState
from django_ray.observability import get_task_summary, get_workflow_plan
from django_ray.runner.reconciliation import mark_task_lost
from django_ray.runtime.compiled_graph import (
    CompiledGraphRuntimeIdentity,
    CompiledGraphSubmissionTransport,
    CompiledGraphTopology,
)
from django_ray.runtime.compiled_graph_lifecycle import (
    COMPILED_GRAPH_LIFECYCLE_PROTOCOL_VERSION,
)
from django_ray.runtime.context import (
    DurableTaskContext,
    WorkflowRunIdentity,
    durable_task_execution,
    get_current_task_context,
)
from django_ray.runtime.runtime_env import normalize_runtime_env
from django_ray.workflow_plans import (
    MAX_PLAN_BYTES,
    MAX_PLAN_NODES,
    MAX_RUNTIME_ENV_DIAGNOSTICS,
    MAX_RUNTIME_ENV_IDENTITY_BYTES,
    PLAN_FORMAT_VERSION,
    PLAN_SELECTION_FORMAT_VERSION,
    PLAN_SELECTION_LEGACY_FORMAT_VERSION,
    WorkflowPlanBuildContext,
    WorkflowPlanMismatchError,
    WorkflowPlanValidationError,
    effective_plan_selection_reporting_policy,
    materialize_workflow_plan,
    plan_requires_drain,
    prepare_materialized_plan_for_ray,
    runtime_env_plan_identity,
    runtime_env_plan_identity_from_transport,
    validate_plan_selection_manifest,
)
from django_ray.workflow_progress import allocate_workflow_run, pin_workflow_plan
from django_ray.workflow_progress_protocol import (
    WorkflowProgressEventKind,
    decode_workflow_progress_event,
)
from django_ray.workflow_progress_summary import deserialize_workflow_progress_summary
from django_ray.workflows import chain, group, map_step, step


def increment(value: int) -> int:
    return value + 1


def double(value: int) -> int:
    return value * 2


def preview_increment(value: int) -> dict[str, int]:
    return {"value": value}


def preview_increment_compact(value: int) -> int:
    return value


callable_alias = increment
SIDE_EFFECTS: list[int] = []


class StatefulCallable:
    def __init__(self, factor: int) -> None:
        self.factor = factor

    def __call__(self, value: int) -> int:
        return value * self.factor


stateful_callable = StatefulCallable(2)


def record_side_effect(value: int) -> int:
    SIDE_EFFECTS.append(value)
    return value


def test_plan_selection_v2_persists_effective_reporting_policy() -> None:
    plan = _materialize(step(increment), 1).plan
    selection = plan.eligibility.select(
        "dynamic_tasks",
        requested_policy="auto",
        reporting_policy="disabled",
    )

    manifest = selection.as_dict()

    assert manifest["plan_selection_format_version"] == PLAN_SELECTION_FORMAT_VERSION
    assert manifest["reporting_policy"] == "disabled"
    assert validate_plan_selection_manifest(manifest) == manifest
    assert effective_plan_selection_reporting_policy(manifest) == "disabled"


def test_local_plan_selection_defaults_to_disabled_reporting() -> None:
    plan = _materialize(step(increment), 1).plan

    manifest = plan.eligibility.select(
        "local",
        requested_policy="local",
    ).as_dict()

    assert manifest["reporting_policy"] == "disabled"
    assert validate_plan_selection_manifest(manifest) == manifest


def test_local_plan_selection_rejects_non_disabled_reporting() -> None:
    plan = _materialize(step(increment), 1).plan

    with pytest.raises(WorkflowPlanValidationError, match="Local workflow execution"):
        plan.eligibility.select(
            "local",
            requested_policy="local",
            reporting_policy="full",
        )

    manifest = plan.eligibility.select(
        "local",
        requested_policy="local",
    ).as_dict()
    manifest["reporting_policy"] = "full"
    with pytest.raises(WorkflowPlanValidationError, match="Local workflow plan selection"):
        validate_plan_selection_manifest(manifest)


def test_plan_selection_v2_accepts_terminal_only_reporting() -> None:
    plan = _materialize(step(increment), 1).plan
    manifest = plan.eligibility.select(
        "dynamic_tasks",
        requested_policy="auto",
        reporting_policy="terminal_only",
    ).as_dict()

    assert validate_plan_selection_manifest(manifest) == manifest
    assert effective_plan_selection_reporting_policy(manifest) == "terminal_only"


def test_plan_selection_v2_reader_accepts_reserved_sampled_reporting() -> None:
    plan = _materialize(step(increment), 1).plan
    manifest = plan.eligibility.select(
        "dynamic_tasks",
        requested_policy="auto",
    ).as_dict()
    manifest["reporting_policy"] = "sampled"

    assert validate_plan_selection_manifest(manifest) == manifest
    assert effective_plan_selection_reporting_policy(manifest) == "sampled"
    with pytest.raises(WorkflowPlanValidationError, match="reporting policy"):
        plan.eligibility.select(
            "dynamic_tasks",
            requested_policy="auto",
            reporting_policy="sampled",
        )


@pytest.mark.parametrize(
    ("selected_strategy", "expected_policy"),
    [("dynamic_tasks", "full"), ("local", "disabled")],
)
def test_plan_selection_v1_reporting_policy_is_inferred_for_rolling_rows(
    selected_strategy: str,
    expected_policy: str,
) -> None:
    plan = _materialize(step(increment), 1).plan
    legacy = plan.eligibility.select(
        selected_strategy,
        requested_policy=selected_strategy,
    ).as_dict()
    legacy["plan_selection_format_version"] = PLAN_SELECTION_LEGACY_FORMAT_VERSION
    legacy.pop("reporting_policy")

    assert validate_plan_selection_manifest(legacy) == legacy
    assert effective_plan_selection_reporting_policy(legacy) == expected_policy


BASE_IMAGE_DIGEST = "sha256:" + "a" * 64
BASE_RUNTIME = CompiledGraphRuntimeIdentity(
    ray_version="2.56.1",
    python_version="3.12.12",
    operating_system="linux",
    architecture="x86_64",
    python_implementation="cpython",
    python_abi="cp312-cp312-manylinux_2_39_x86_64",
    dependency_profile="ray=2.56.1;django=6.0.7",
    platform_profile="linux-x86_64",
    libc_profile="glibc-2.39",
    container_profile="django-ray:test",
    deployment_profile=BASE_IMAGE_DIGEST,
    shared_memory_profile="posix-dev-shm:size=68719476736:mount=tmpfs",
    object_store_profile="capacity=68719476736:spill=disabled",
)
BASE_CONTEXT = WorkflowPlanBuildContext(
    build_revision="build:sha256:0123456789abcdef",
    container_image_digest=BASE_IMAGE_DIGEST,
    compiled_graph_runtime=BASE_RUNTIME,
    compiled_graph_topology=CompiledGraphTopology.NESTED_RAY_TASK,
    compiled_graph_submission_transport=(CompiledGraphSubmissionTransport.DIRECT_RAY_CORE),
)


def _materialize(signature, *args, context=BASE_CONTEXT, **kwargs):
    return materialize_workflow_plan(
        signature,
        invocation_args=args,
        invocation_kwargs=kwargs,
        build_context=context,
    )


def _task_context(execution: RayTaskExecution) -> DurableTaskContext:
    return DurableTaskContext(
        task_pk=execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
    )


def _allocate(
    execution: RayTaskExecution,
    plan,
    selection,
) -> WorkflowRunIdentity:
    identity = allocate_workflow_run(
        _task_context(execution),
        plan=plan,
        selection=selection,
    )
    assert identity is not None
    return identity


@pytest.mark.django_db
def test_idempotent_plan_pin_refreshes_activity_and_fences_stale_lost() -> None:
    observed_heartbeat = datetime.now(UTC) - timedelta(minutes=10)
    execution = RayTaskExecution.objects.create(
        task_id="workflow-plan-pin-activity",
        callable_path=f"{__name__}.increment",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=5,
        last_heartbeat_at=observed_heartbeat,
    )
    plan = _materialize(step(increment), 1).plan
    selection = plan.eligibility.select("dynamic_tasks", requested_policy="auto")
    context = DurableTaskContext(
        task_pk=execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
    )
    assert pin_workflow_plan(context, plan, selection) is True
    RayTaskExecution.objects.filter(pk=execution.pk).update(last_heartbeat_at=observed_heartbeat)
    stale_lost_snapshot = RayTaskExecution.objects.get(pk=execution.pk)

    assert pin_workflow_plan(context, plan, selection) is True
    assert mark_task_lost(stale_lost_snapshot) is False

    execution.refresh_from_db()
    assert execution.state == TaskState.RUNNING
    assert execution.last_heartbeat_at is not None
    assert execution.last_heartbeat_at > observed_heartbeat


def test_semantically_equal_plans_have_byte_equal_canonical_identity() -> None:
    first = step(
        increment,
        ray_options={"resources": {"zeta": 1.0, "alpha": 2}, "num_cpus": 1.0},
    )
    second = step(
        increment,
        ray_options={"num_cpus": 1, "resources": {"alpha": 2.0, "zeta": 1}},
    )

    first_plan = _materialize(first, {"namespace": "one"}).plan
    second_plan = _materialize(second, {"namespace": "two"}).plan

    assert first_plan.canonical_json == second_plan.canonical_json
    assert first_plan.fingerprint == second_plan.fingerprint
    assert first_plan.fingerprint.startswith("sha256:")


@pytest.mark.parametrize(
    "ray_options",
    [
        {"num_cpus": 1},
        {"num_returns": 1},
        {"placement_group": "default"},
        {"placement_group_bundle_index": -1},
        {"placement_group_capture_child_tasks": False},
        {"resources": None},
        {"max_retries": 3},
        {"enable_task_events": True},
        {"retry_exceptions": False},
        {"scheduling_strategy": "DEFAULT"},
    ],
)
def test_explicit_ray_defaults_match_omitted_options(ray_options) -> None:
    baseline = _materialize(step(increment), 1).plan
    explicit = _materialize(step(increment, ray_options=ray_options), 1).plan

    assert baseline.fingerprint == explicit.fingerprint


def test_invocation_and_bound_values_do_not_change_topology_identity() -> None:
    first = _materialize(step(double, factor={"namespace": "alpha"}), "request-a").plan
    second = _materialize(step(double, factor={"namespace": "beta"}), "request-b").plan

    assert first.fingerprint == second.fingerprint
    assert "request-a" not in first.canonical_json
    assert "factor" in first.canonical_json  # keyword slot name, not its value
    assert "alpha" not in first.canonical_json


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (step(increment), step(double)),
        (step(increment), step(increment, ray_options={"num_cpus": 2})),
        (chain(step(increment), step(double)), group(step(increment), step(double))),
        (map_step(increment), map_step(increment).with_limits(max_concurrency=2)),
    ],
)
def test_callable_resource_topology_and_admission_changes_invalidate(first, second) -> None:
    assert _materialize(first, 1).plan.fingerprint != _materialize(second, 1).plan.fingerprint


def test_output_preview_contract_is_explicit_and_fingerprinted() -> None:
    baseline = _materialize(step(increment), 1).plan
    previewed = _materialize(
        step(increment).with_output_preview(preview_increment),
        1,
    ).plan
    compact = _materialize(
        step(increment).with_output_preview(preview_increment_compact),
        1,
    ).plan

    assert baseline.fingerprint != previewed.fingerprint
    assert previewed.fingerprint != compact.fingerprint
    assert "output_preview" not in baseline.manifest["nodes"][0]
    assert previewed.manifest["nodes"][0]["output_preview"] == {
        "mode": "author_projection",
        "callable": {"ref": "callable:1"},
        "limits_profile": "v1",
    }
    assert previewed.manifest["nodes"][0]["outputs"] == ("result",)
    assert [entry["import_path"] for entry in previewed.manifest["callables"]] == [
        "tests.unit.test_workflow_plans.increment",
        "tests.unit.test_workflow_plans.preview_increment",
    ]


def test_result_retention_bounds_match_map_execution_behavior() -> None:
    unbounded = _materialize(map_step(increment), [1, 2]).plan
    bounded = _materialize(
        map_step(increment).with_limits(max_items=7, max_concurrency=2),
        [1, 2],
    ).plan
    static_group = _materialize(
        group(step(increment), step(increment), step(increment)),
        1,
    ).plan

    assert unbounded.manifest["capabilities"]["admission"]["maximum_buffered_results"] is None
    assert bounded.manifest["capabilities"]["admission"]["maximum_buffered_results"] == 7
    assert static_group.manifest["capabilities"]["admission"]["maximum_buffered_results"] == 3
    assert any(
        rejection.code == "UNBOUNDED_ADMISSION" for rejection in unbounded.eligibility.rejections
    )


def test_unselected_map_keeps_the_v1_extensibility_slots_unchanged() -> None:
    plan = _materialize(
        map_step(increment).with_limits(max_items=7, max_concurrency=2),
        [1, 2],
    ).plan
    manifest = plan.as_dict()
    map_node = next(node for node in manifest["nodes"] if node["operation"] == "dynamic_map")

    assert PLAN_FORMAT_VERSION == 1
    assert map_node["actor_layout"] is None
    assert manifest["physical_topology"] == {
        "node_model": "ray_tasks",
        "stages": [],
        "actors": [],
        "placement_relationships": [],
    }
    assert "django-ray.workflow-map-result-buffer" not in plan.canonical_json


def test_result_buffer_uses_versioned_actor_topology_extensibility_slots() -> None:
    signature = (
        map_step(increment)
        .with_limits(
            max_items=7,
            max_concurrency=2,
        )
        .with_result_buffer(
            max_serialized_bytes=8192,
            actor_options={
                "num_cpus": 0.25,
                "memory": 16384,
                "resources": {"result_buffer": 1},
                "scheduling_strategy": "SPREAD",
            },
        )
    )

    plan = _materialize(signature, [1, 2]).plan
    manifest = plan.as_dict()
    topology = manifest["physical_topology"]
    map_node = next(node for node in manifest["nodes"] if node["operation"] == "dynamic_map")
    result_node = next(
        node for node in manifest["nodes"] if node["operation"] == "ordered_actor_finalize"
    )
    contract = topology["actors"][0]["contract"]

    assert manifest["plan_format_version"] == 1
    assert topology["node_model"] == "ray_tasks_and_actors"
    assert len(topology["actors"]) == 1
    assert topology["actors"][0]["id"] == "0.result_buffer"
    assert topology["actors"][0]["resources"] == {
        "num_cpus": 0.25,
        "memory": 16384,
        "custom": {"result_buffer": 1},
    }
    assert topology["placement_relationships"] == [
        {
            "source": "0",
            "target": "0.result_buffer",
            "relationship": "owns_non_detached_actor",
            "placement": {
                "scheduling_strategy": "SPREAD",
                "custom_resources": {"result_buffer": 1},
            },
        }
    ]
    assert map_node["actor_layout"] == "0.result_buffer"
    assert result_node["actor_layout"] == "0.result_buffer"
    assert result_node["node_model"] == "actor"
    assert contract["protocol_version"] == 1
    assert contract["codec"]["name"] == "ray.cloudpickle"
    assert contract["codec"]["version"] == 1
    assert contract["bounds"] == {
        "maximum_items": 7,
        "maximum_in_flight_leaves": 2,
        "maximum_serialized_bytes": 8192,
        "maximum_pending_actor_calls": 2,
    }
    assert contract["lifetime"]["kind"] == "non_detached"
    assert contract["restart"] == {"max_restarts": 0, "max_task_retries": 0}


def test_every_result_buffer_bound_resource_and_placement_changes_fingerprint() -> None:
    def buffered(
        *,
        max_items: int = 7,
        max_concurrency: int = 2,
        max_serialized_bytes: int = 8192,
        num_cpus: float = 0.25,
        memory: int = 16384,
        resource: float = 1,
        strategy: str = "DEFAULT",
    ):
        return (
            map_step(increment)
            .with_limits(
                max_items=max_items,
                max_concurrency=max_concurrency,
            )
            .with_result_buffer(
                max_serialized_bytes=max_serialized_bytes,
                actor_options={
                    "num_cpus": num_cpus,
                    "memory": memory,
                    "resources": {"result_buffer": resource},
                    "scheduling_strategy": strategy,
                },
            )
        )

    plans = [
        _materialize(buffered(), [1]).plan,
        _materialize(buffered(max_items=8), [1]).plan,
        _materialize(buffered(max_concurrency=3), [1]).plan,
        _materialize(buffered(max_serialized_bytes=9000), [1]).plan,
        _materialize(buffered(num_cpus=0.5), [1]).plan,
        _materialize(buffered(memory=32768), [1]).plan,
        _materialize(buffered(resource=2), [1]).plan,
        _materialize(buffered(strategy="SPREAD"), [1]).plan,
    ]

    assert len({plan.fingerprint for plan in plans}) == len(plans)


def test_compiler_platform_and_deployment_identity_are_fingerprinted() -> None:
    baseline = _materialize(step(increment), 1).plan
    compiler_change = _materialize(
        step(increment),
        1,
        context=replace(
            BASE_CONTEXT,
            compiled_graph_settings={"buffer_bytes": 4096},
        ),
    ).plan
    platform_change = _materialize(
        step(increment),
        1,
        context=replace(
            BASE_CONTEXT,
            compiled_graph_runtime=replace(BASE_RUNTIME, architecture="aarch64"),
        ),
    ).plan
    deployment_change = _materialize(
        step(increment),
        1,
        context=replace(BASE_CONTEXT, build_revision="build:replacement"),
    ).plan
    image_change = _materialize(
        step(increment),
        1,
        context=replace(
            BASE_CONTEXT,
            container_image_digest="sha256:" + "b" * 64,
        ),
    ).plan

    assert (
        len(
            {
                baseline.fingerprint,
                compiler_change.fingerprint,
                platform_change.fingerprint,
                deployment_change.fingerprint,
                image_change.fingerprint,
            }
        )
        == 5
    )
    manifest = baseline.as_dict()
    assert manifest["physical_topology"]["actors"] == []
    assert manifest["capabilities"]["transport"]["kind"] == "ray_object_store"
    assert manifest["definition"]["build_revision"] == BASE_CONTEXT.build_revision
    assert manifest["definition"]["container_image_digest"] == BASE_IMAGE_DIGEST


def test_compiled_graph_lifecycle_protocol_is_fingerprinted_and_fail_closed(
    monkeypatch,
) -> None:
    import django_ray.workflow_plans as plan_module

    baseline = _materialize(step(increment), 1).plan
    requirements = baseline.as_dict()["strategy_requirements"]["compiled_graph"]

    assert (
        requirements["lifecycle_protocol_version"] == COMPILED_GRAPH_LIFECYCLE_PROTOCOL_VERSION == 1
    )
    with pytest.raises(
        WorkflowPlanValidationError,
        match="compiled_graph_settings.lifecycle_protocol_version must be 1",
    ):
        _materialize(
            step(increment),
            1,
            context=replace(
                BASE_CONTEXT,
                compiled_graph_settings={"lifecycle_protocol_version": 2},
            ),
        )

    monkeypatch.setattr(plan_module, "COMPILED_GRAPH_LIFECYCLE_PROTOCOL_VERSION", 2)
    changed = _materialize(
        step(increment),
        1,
        context=replace(
            BASE_CONTEXT,
            compiled_graph_settings={"lifecycle_protocol_version": 2},
        ),
    ).plan

    assert (
        changed.manifest["strategy_requirements"]["compiled_graph"]["lifecycle_protocol_version"]
        == 2
    )
    assert changed.fingerprint != baseline.fingerprint


@pytest.mark.parametrize(
    "field",
    ["maximum_in_flight", "maximum_buffered_results", "owner_concurrency"],
)
def test_compiled_graph_lifecycle_v1_rejects_wider_capacity(field: str) -> None:
    with pytest.raises(
        WorkflowPlanValidationError,
        match=rf"compiled_graph_settings.{field} must be 1",
    ):
        _materialize(
            step(increment),
            1,
            context=replace(
                BASE_CONTEXT,
                compiled_graph_settings={field: 2},
            ),
        )


@pytest.mark.parametrize("field", ["settings_version", "lifecycle_protocol_version"])
def test_compiled_graph_version_fields_reject_boolean_aliases(field: str) -> None:
    with pytest.raises(
        WorkflowPlanValidationError,
        match=rf"compiled_graph_settings.{field} must be 1",
    ):
        _materialize(
            step(increment),
            1,
            context=replace(
                BASE_CONTEXT,
                compiled_graph_settings={field: True},
            ),
        )


def test_compiled_graph_compatibility_uses_the_versioned_adapter_record() -> None:
    plan = _materialize(step(increment), 1).plan

    compatibility = plan.as_dict()["compatibility"]["compiled_graph"]

    assert "message" not in compatibility
    assert compatibility["schema_version"] == 2
    assert compatibility["policy_version"] == 3
    assert compatibility["reason"] == "CANDIDATE_REQUIRES_SMOKE"
    assert compatibility["plan_rejection_code"] == "INCOMPATIBLE_PLATFORM"
    assert compatibility["topology"] == "nested-ray-task"
    assert compatibility["submission_transport"] == "direct-ray-core"
    assert compatibility["transport"] == "cpu-shared-memory"
    assert compatibility["runtime"]["deployment_profile"] == BASE_IMAGE_DIGEST
    assert any(
        rejection.code == "INCOMPATIBLE_PLATFORM"
        and rejection.path == "compatibility.compiled_graph.capability_set"
        for rejection in plan.eligibility.rejections
    )


@pytest.mark.parametrize(
    ("runtime", "reason", "path"),
    [
        (
            replace(BASE_RUNTIME, ray_version=None),
            "RAY_NOT_INSTALLED",
            "compatibility.compiled_graph.runtime.ray_version",
        ),
        (
            replace(BASE_RUNTIME, ray_version="9.0.0"),
            "UNSUPPORTED_RAY_VERSION",
            "compatibility.compiled_graph.runtime.ray_version",
        ),
        (
            replace(BASE_RUNTIME, python_version="3.11.9"),
            "UNSUPPORTED_PYTHON",
            "compatibility.compiled_graph.runtime.python_version",
        ),
        (
            replace(BASE_RUNTIME, operating_system="windows"),
            "UNSUPPORTED_OPERATING_SYSTEM",
            "compatibility.compiled_graph.runtime.operating_system",
        ),
        (
            replace(BASE_RUNTIME, architecture="aarch64"),
            "UNSUPPORTED_ARCHITECTURE",
            "compatibility.compiled_graph.runtime.architecture",
        ),
        (
            replace(BASE_RUNTIME, shared_memory_profile="unresolved"),
            "INCOMPLETE_CAPABILITY_CONTEXT",
            "compatibility.compiled_graph.runtime",
        ),
    ],
)
def test_compiled_graph_rejection_reason_maps_to_stable_plan_path(
    runtime,
    reason,
    path,
) -> None:
    plan = _materialize(
        step(increment),
        1,
        context=replace(BASE_CONTEXT, compiled_graph_runtime=runtime),
    ).plan

    assert plan.manifest["compatibility"]["compiled_graph"]["reason"] == reason
    assert any(
        rejection.code == "INCOMPATIBLE_PLATFORM" and rejection.path == path
        for rejection in plan.eligibility.rejections
    )


def test_compiled_graph_transport_uses_canonical_policy_vocabulary() -> None:
    gpu_plan = _materialize(
        step(increment),
        1,
        context=replace(
            BASE_CONTEXT,
            compiled_graph_settings={"transport": "gpu-nccl"},
        ),
    ).plan

    assert gpu_plan.manifest["compatibility"]["compiled_graph"]["reason"] == "UNSUPPORTED_TRANSPORT"
    assert any(
        rejection.code == "UNSUPPORTED_TRANSPORT"
        and rejection.path == "compatibility.compiled_graph.transport"
        for rejection in gpu_plan.eligibility.rejections
    )
    with pytest.raises(WorkflowPlanValidationError, match="cpu-shared-memory or gpu-nccl"):
        _materialize(
            step(increment),
            1,
            context=replace(
                BASE_CONTEXT,
                compiled_graph_settings={"transport": "shared_memory"},
            ),
        )


def test_deployment_digest_disagreement_rejects_reuse() -> None:
    context = replace(
        BASE_CONTEXT,
        container_image_digest="sha256:" + "b" * 64,
    )

    plan = _materialize(step(increment), 1, context=context).plan

    assert any(
        rejection.code == "INCOMPATIBLE_PLATFORM"
        and rejection.path == "compatibility.compiled_graph.runtime.deployment_profile"
        for rejection in plan.eligibility.rejections
    )


@pytest.mark.parametrize(
    ("deployment_profile", "reason"),
    [
        ("mutable:latest", "INCOMPLETE_CAPABILITY_CONTEXT"),
        ("x" * 1025, "INVALID_RUNTIME_IDENTITY"),
    ],
)
def test_container_image_does_not_hide_invalid_runtime_deployment_profile(
    deployment_profile,
    reason,
) -> None:
    runtime = replace(BASE_RUNTIME, deployment_profile=deployment_profile)
    context = replace(BASE_CONTEXT, compiled_graph_runtime=runtime)

    plan = _materialize(step(increment), 1, context=context).plan
    compatibility = plan.manifest["compatibility"]["compiled_graph"]

    assert compatibility["reason"] == reason
    if reason == "INCOMPLETE_CAPABILITY_CONTEXT":
        assert compatibility["runtime"]["deployment_profile"] == deployment_profile
    else:
        assert compatibility["runtime"]["deployment_profile"].startswith("<oversized:")


def test_default_deployment_identity_keeps_build_and_image_components(
    settings,
    monkeypatch,
) -> None:
    import django_ray.workflow_plans as plan_module

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "WORKFLOW_PLAN_CODE_REVISION": None,
    }
    monkeypatch.setenv("DJANGO_RAY_BUILD_REVISION", "build-17")
    monkeypatch.setenv("DJANGO_RAY_IMAGE_DIGEST", "sha256:" + "A" * 64)
    monkeypatch.setattr(
        plan_module,
        "detect_compiled_graph_runtime",
        lambda: replace(BASE_RUNTIME, deployment_profile="unresolved"),
    )

    plan = materialize_workflow_plan(step(increment), invocation_args=(1,)).plan

    assert plan.manifest["definition"]["build_revision"] == (
        "environment:DJANGO_RAY_BUILD_REVISION:build-17"
    )
    assert plan.manifest["definition"]["container_image_digest"] == BASE_IMAGE_DIGEST
    assert (
        plan.manifest["compatibility"]["compiled_graph"]["runtime"]["deployment_profile"]
        == BASE_IMAGE_DIGEST
    )


def test_default_deployment_identity_rejects_malformed_image_digest(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DJANGO_RAY_IMAGE_DIGEST", "container:latest")

    with pytest.raises(
        WorkflowPlanValidationError,
        match="DJANGO_RAY_IMAGE_DIGEST must be a sha256",
    ):
        materialize_workflow_plan(step(increment), invocation_args=(1,))


def test_missing_deployment_revision_rejects_reusable_code_identity() -> None:
    plan = _materialize(
        step(increment),
        1,
        context=replace(BASE_CONTEXT, build_revision=None),
    ).plan

    assert any(
        rejection.code == "UNRESOLVED_CODE_IDENTITY"
        and rejection.path == "definition.build_revision"
        for rejection in plan.eligibility.rejections
    )


def test_named_runtime_env_is_resolved_once_and_content_changes_identity(settings) -> None:
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "RUNTIME_ENV_PROFILES": {"worker": {"pip": ["numpy==2.1.0"]}},
    }
    first = _materialize(step(increment, runtime_env="worker"), 1)

    settings.DJANGO_RAY["RUNTIME_ENV_PROFILES"]["worker"]["pip"][0] = "numpy==2.2.0"
    second = _materialize(step(increment, runtime_env="worker"), 1)

    assert first.plan.fingerprint != second.plan.fingerprint
    assert json.loads(first.binding_for_node("0").runtime_env_serialized or "{}")["pip"] == [
        "numpy==2.1.0"
    ]


def test_runtime_env_profile_aliases_do_not_change_semantic_plan_identity(settings) -> None:
    spec = {"env_vars": {"MODE": "production"}}
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "RUNTIME_ENV_PROFILES": {"primary": spec, "alias": deepcopy(spec)},
    }

    primary = _materialize(step(increment, runtime_env="primary"), 1)
    alias = _materialize(step(increment, runtime_env="alias"), 1)

    assert primary.plan.canonical_json == alias.plan.canonical_json
    assert primary.plan.fingerprint == alias.plan.fingerprint
    assert "primary" not in primary.plan.canonical_json
    assert "alias" not in alias.plan.canonical_json
    primary_binding = primary.binding_for_node("0")
    alias_binding = alias.binding_for_node("0")
    assert primary_binding is not None
    assert alias_binding is not None
    assert primary_binding.runtime_env_profile == "primary"
    assert alias_binding.runtime_env_profile == "alias"
    assert primary_binding.runtime_env_metadata["profile"] == "primary"
    assert alias_binding.runtime_env_metadata["profile"] == "alias"
    primary_identity = runtime_env_plan_identity(normalize_runtime_env(spec, profile="primary"))
    alias_identity = runtime_env_plan_identity(normalize_runtime_env(spec, profile="alias"))
    assert primary_identity.manifest["digest"] == alias_identity.manifest["digest"]
    assert (
        primary_identity.manifest["transport_digest"] != alias_identity.manifest["transport_digest"]
    )


@pytest.mark.parametrize(
    ("task_context", "topology", "submission_transport", "reason", "path"),
    [
        (
            DurableTaskContext(
                task_pk=1,
                compiled_graph_submission_transport="direct-ray-core",
            ),
            "nested-ray-task",
            "direct-ray-core",
            "CANDIDATE_REQUIRES_SMOKE",
            "compatibility.compiled_graph.capability_set",
        ),
        (
            DurableTaskContext(
                task_pk=1,
                compiled_graph_submission_transport="ray-client",
            ),
            "nested-ray-task",
            "ray-client",
            "UNSUPPORTED_SUBMISSION_TRANSPORT",
            "compatibility.compiled_graph.submission_transport",
        ),
        (
            DurableTaskContext(task_pk=1, ray_job_driver=True),
            "ray-job-driver",
            "ray-job",
            "CANDIDATE_REQUIRES_SMOKE",
            "compatibility.compiled_graph.capability_set",
        ),
        (
            DurableTaskContext(task_pk=1),
            "",
            "",
            "UNSUPPORTED_TOPOLOGY",
            "compatibility.compiled_graph.topology",
        ),
    ],
)
def test_default_context_carries_actual_submission_transport(
    settings,
    monkeypatch,
    task_context,
    topology,
    submission_transport,
    reason,
    path,
) -> None:
    import django_ray.workflow_plans as plan_module

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "WORKFLOW_PLAN_CODE_REVISION": "build:test",
    }
    monkeypatch.setattr(
        plan_module,
        "detect_compiled_graph_runtime",
        lambda: BASE_RUNTIME,
    )

    plan = materialize_workflow_plan(
        step(increment),
        invocation_args=(1,),
        task_context=task_context,
    ).plan
    record = plan.manifest["compatibility"]["compiled_graph"]

    assert record["topology"] == topology
    assert record["submission_transport"] == submission_transport
    assert record["reason"] == reason
    assert any(rejection.path == path for rejection in plan.eligibility.rejections)


def test_context_free_materialization_defers_direct_driver_identity(
    settings,
    monkeypatch,
) -> None:
    import django_ray.workflow_plans as plan_module

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "WORKFLOW_PLAN_CODE_REVISION": "build:test",
    }
    monkeypatch.setattr(
        plan_module,
        "detect_compiled_graph_runtime",
        lambda: BASE_RUNTIME,
    )

    plan = materialize_workflow_plan(step(increment), invocation_args=(1,)).plan
    compatibility = plan.manifest["compatibility"]["compiled_graph"]

    assert compatibility["topology"] == ""
    assert compatibility["submission_transport"] == ""
    assert compatibility["reason"] == "UNSUPPORTED_TOPOLOGY"
    assert any(
        rejection.code == "OWNER_LIFETIME_MISMATCH"
        and rejection.path == "compatibility.compiled_graph.topology"
        for rejection in plan.eligibility.rejections
    )


def test_secret_values_never_enter_plan_or_secret_derived_fingerprint() -> None:
    trust = {
        "trust_domain": "cluster:production",
        "credential_provider": "kubernetes-service-account",
        "credential_revision": "provider-v3",
        "service_account_audience": "kubernetes.default.svc",
    }
    context = replace(BASE_CONTEXT, trust_identity=trust)
    first = _materialize(
        step(
            increment,
            runtime_env={
                "env_vars": {"API_TOKEN": "low-entropy-secret"},
                "pip": ["https://user:password-in-url@host-low-entropy.example.invalid/pkg.whl"],
                "conda": {
                    "channels": ["tenant-low-entropy"],
                    "dependencies": ["python=3.12"],
                    "variables": {"DATABASE_PASSWORD": "plain-conda-secret"},
                },
                "image_uri": "tenant-low-entropy/image@sha256:" + "a" * 64,
                "config": {
                    "setup_timeout_seconds": 30,
                    "log_files": "secret-log-path",
                    "tenant-low-entropy-field": 1,
                },
                "tenant-low-entropy-runtime-field": "secret-field-value",
            },
        ),
        1,
        context=context,
    ).plan
    rotated = _materialize(
        step(
            increment,
            runtime_env={
                "env_vars": {"API_TOKEN": "rotated-token"},
                "pip": ["https://other:replacement@host-rotated.example.invalid/pkg.whl"],
                "conda": {
                    "channels": ["tenant-rotated"],
                    "dependencies": ["python=3.12"],
                    "variables": {"DATABASE_PASSWORD": "rotated-conda-secret"},
                },
                "image_uri": "tenant-rotated/image@sha256:" + "a" * 64,
                "config": {
                    "setup_timeout_seconds": 30,
                    "log_files": "rotated-log-path",
                    "tenant-rotated-field": 1,
                },
                "tenant-rotated-runtime-field": "rotated-field-value",
            },
        ),
        1,
        context=context,
    ).plan

    assert "low-entropy-secret" not in first.canonical_json
    assert "password-in-url" not in first.canonical_json
    assert "host-low-entropy" not in first.canonical_json
    assert "tenant-low-entropy" not in first.canonical_json
    assert "plain-conda-secret" not in first.canonical_json
    assert "secret-log-path" not in first.canonical_json
    assert "rotated-token" not in rotated.canonical_json
    assert first.fingerprint == rotated.fingerprint
    # URI credentials are unresolved compatibility data, so their rotation does
    # not create a digest of the credential and reusable strategies stay rejected.
    codes = {rejection.code for rejection in first.eligibility.rejections}
    assert "UNRESOLVED_RUNTIME_ENV" in codes


def test_provider_revision_invalidates_while_token_rotation_does_not() -> None:
    base_trust = {
        "credential_provider": "workload-identity",
        "credential_revision": "revision-1",
    }
    first = _materialize(
        step(increment, runtime_env={"env_vars": {"TOKEN": "first"}}),
        1,
        context=replace(BASE_CONTEXT, trust_identity=base_trust),
    ).plan
    rotated = _materialize(
        step(increment, runtime_env={"env_vars": {"TOKEN": "second"}}),
        1,
        context=replace(BASE_CONTEXT, trust_identity=base_trust),
    ).plan
    provider_change = _materialize(
        step(increment, runtime_env={"env_vars": {"TOKEN": "second"}}),
        1,
        context=replace(
            BASE_CONTEXT,
            trust_identity={**base_trust, "credential_revision": "revision-2"},
        ),
    ).plan

    assert first.fingerprint == rotated.fingerprint
    assert provider_change.fingerprint != first.fingerprint
    assert first.retry_safe is True


def test_ordinary_environment_values_require_a_declared_environment_revision() -> None:
    trust = {
        "credential_provider": "workload-identity",
        "credential_revision": "revision-1",
    }
    production = _materialize(
        step(
            increment,
            runtime_env={"env_vars": {"MODE": "production", "API_TOKEN": "first"}},
        ),
        1,
        context=replace(BASE_CONTEXT, trust_identity=trust),
    ).plan
    development = _materialize(
        step(
            increment,
            runtime_env={"env_vars": {"MODE": "development", "API_TOKEN": "rotated"}},
        ),
        1,
        context=replace(BASE_CONTEXT, trust_identity=trust),
    ).plan
    token_only_rotation = _materialize(
        step(
            increment,
            runtime_env={"env_vars": {"MODE": "production", "API_TOKEN": "rotated"}},
        ),
        1,
        context=replace(BASE_CONTEXT, trust_identity=trust),
    ).plan

    assert production.fingerprint == development.fingerprint
    assert production.fingerprint == token_only_rotation.fingerprint
    assert "production" not in production.canonical_json
    assert "development" not in development.canonical_json
    assert {rejection.code for rejection in production.eligibility.rejections} >= {
        "UNRESOLVED_RUNTIME_ENV"
    }
    assert production.retry_safe is False


def test_innocuous_variable_names_cannot_create_secret_derived_identity() -> None:
    first = _materialize(
        step(increment, runtime_env={"env_vars": {"FOO": "low-entropy-secret"}}),
        1,
    ).plan
    rotated = _materialize(
        step(increment, runtime_env={"env_vars": {"FOO": "rotated-secret"}}),
        1,
    ).plan

    assert first.fingerprint == rotated.fingerprint
    assert "low-entropy-secret" not in first.canonical_json
    assert any(
        rejection.code == "UNRESOLVED_RUNTIME_ENV" for rejection in first.eligibility.rejections
    )


def test_installer_flags_cannot_create_secret_derived_identity() -> None:
    artifact_hash = "a" * 64
    first = _materialize(
        step(
            increment,
            runtime_env={
                "pip": [
                    f"pkg==1 --config-settings=auth_token=lowentropy --hash=sha256:{artifact_hash}"
                ]
            },
        ),
        1,
    ).plan
    rotated = _materialize(
        step(
            increment,
            runtime_env={
                "pip": [
                    f"pkg==1 --config-settings=auth_token=rotated --hash=sha256:{artifact_hash}"
                ]
            },
        ),
        1,
    ).plan

    assert first.fingerprint == rotated.fingerprint
    assert "lowentropy" not in first.canonical_json
    assert any(
        rejection.code == "UNRESOLVED_RUNTIME_ENV" for rejection in first.eligibility.rejections
    )


def test_declared_environment_revision_can_cover_runtime_only_values() -> None:
    first = _materialize(
        step(increment, runtime_env={"env_vars": {"MODE": "production"}}),
        1,
        context=replace(
            BASE_CONTEXT,
            trust_identity={"environment_revision": "environment-v1"},
        ),
    ).plan
    replacement = _materialize(
        step(increment, runtime_env={"env_vars": {"MODE": "development"}}),
        1,
        context=replace(
            BASE_CONTEXT,
            trust_identity={"environment_revision": "environment-v2"},
        ),
    ).plan

    assert first.fingerprint != replacement.fingerprint
    assert first.retry_safe is True
    assert replacement.retry_safe is True


def test_exact_but_mutable_dependency_spec_is_retry_unsafe() -> None:
    plan = _materialize(
        step(increment, runtime_env={"pip": ["mutable-package==1.0"]}),
        1,
    ).plan

    assert plan.retry_safe is False
    assert plan.retry_unsafe_paths == ("environments.by_node.0.spec.pip.0",)


def test_runtime_env_transport_is_bounded_and_strictly_revalidated() -> None:
    resolved = normalize_runtime_env(
        {"excludes": [f"{'x' * 2040}{index:04d}" for index in range(1024)]}
    )
    identity = runtime_env_plan_identity(resolved)
    transported = identity.as_transport_dict()

    assert len(json.dumps(transported).encode("utf-8")) <= MAX_RUNTIME_ENV_IDENTITY_BYTES
    assert runtime_env_plan_identity_from_transport(transported).as_dict() == transported
    assert transported["retry_safe"] is False
    assert transported["retry_unsafe_paths"] == ["spec.excludes"]

    malicious = {**transported, "spec": {"env_vars": {"API_TOKEN": "leaked"}}}
    with pytest.raises(WorkflowPlanValidationError, match="unsupported schema"):
        runtime_env_plan_identity_from_transport(malicious)
    with pytest.raises(WorkflowPlanValidationError, match="unsupported schema"):
        materialize_workflow_plan(
            step(increment),
            invocation_args=(1,),
            task_context=DurableTaskContext(
                task_pk=1,
                runtime_env_plan_identity=malicious,
            ),
            build_context=BASE_CONTEXT,
        )
    inconsistent = {
        **transported,
        "retry_safe": True,
    }
    with pytest.raises(WorkflowPlanValidationError, match="retry-safety metadata"):
        runtime_env_plan_identity_from_transport(inconsistent)


def test_runtime_env_transport_accepts_retry_paths_beyond_unresolved_diagnostic_window(
    tmp_path,
) -> None:
    modules: list[str] = []
    for index in range(MAX_RUNTIME_ENV_DIAGNOSTICS + 1):
        module = tmp_path / f"module-{index}.py"
        module.write_text(f"VALUE = {index}\n", encoding="utf-8")
        modules.append(str(module))
    identity = runtime_env_plan_identity(
        normalize_runtime_env(
            {
                "py_modules": modules,
                "uv": ["mutable-package==1.0"],
            }
        )
    )
    transported = identity.as_transport_dict()

    assert transported["unresolved_paths_truncated"] is True
    assert transported["retry_unsafe_paths"] == ["spec.uv.0"]
    assert "spec.uv.0" not in transported["unresolved_paths"]
    assert runtime_env_plan_identity_from_transport(transported).as_dict() == transported


def test_callable_reexport_target_is_part_of_identity(monkeypatch) -> None:
    import_path = f"{__name__}.callable_alias"
    first = _materialize(step(import_path), 1).plan
    monkeypatch.setattr(sys.modules[__name__], "callable_alias", double)
    second = _materialize(step(import_path), 1).plan

    assert first.fingerprint != second.fingerprint


def test_worker_only_runtime_env_callable_remains_dynamic_compatible(tmp_path) -> None:
    source = tmp_path / "worker-code"
    source.mkdir()
    (source / "worker_only_tasks.py").write_text(
        "def transform(value):\n    return value + 1\n",
        encoding="utf-8",
    )

    materialized = _materialize(
        step(
            "worker_only_tasks.transform",
            runtime_env={"working_dir": str(source)},
        ),
        1,
    )

    callable_identity = materialized.plan.manifest["callables"][0]["code_identity"]
    assert callable_identity["kind"] == "unknown"
    assert materialized.binding_for_node("0").runtime_env_serialized is not None
    assert any(
        rejection.code == "UNRESOLVED_CODE_IDENTITY"
        for rejection in materialized.plan.eligibility.rejections
    )


def test_submitter_import_side_effect_failure_remains_dynamic_compatible(monkeypatch) -> None:
    import django_ray.workflow_plans as plan_module

    monkeypatch.setattr(
        plan_module,
        "import_callable",
        lambda _path: (_ for _ in ()).throw(RuntimeError("worker-only dependency missing")),
    )

    materialized = _materialize(step("worker_only_tasks.transform"), 1)

    assert materialized.plan.manifest["callables"][0]["kind"] == "unknown"
    assert any(
        rejection.code == "UNRESOLVED_CODE_IDENTITY"
        for rejection in materialized.plan.eligibility.rejections
    )


def test_distinct_callables_from_one_module_share_one_content_digest(monkeypatch) -> None:
    import django_ray.workflow_plans as plan_module

    original_source_digest = plan_module._source_digest
    source_digest_calls = 0

    def count_source_digest(path: str, size: int) -> str:
        nonlocal source_digest_calls
        source_digest_calls += 1
        return original_source_digest(path, size)

    monkeypatch.setattr(plan_module, "_source_digest", count_source_digest)

    _materialize(chain(step(increment), step(double)), 1)

    assert source_digest_calls == 1


def test_stateful_callable_instance_is_never_marked_reusable(monkeypatch) -> None:
    callable_path = f"{__name__}.stateful_callable"
    first = _materialize(step(callable_path), 2).plan
    monkeypatch.setattr(stateful_callable, "factor", 3)
    mutated = _materialize(step(callable_path), 2).plan

    assert first.fingerprint == mutated.fingerprint
    assert first.manifest["callables"][0]["kind"] == "callable_object"
    assert any(
        rejection.code == "UNRESOLVED_CODE_IDENTITY" for rejection in first.eligibility.rejections
    )


@pytest.mark.parametrize(
    "uri",
    [
        "gcs://_ray_pkg_0123456789abcdef.zip",
        "gcs://_ray_pkg_0123456789abcdef0123456789abcdef01234567.zip",
        "gcs://_ray_pkg_0123456789abcdef0123456789abcdef01234567.tar.gz",
    ],
)
def test_installed_ray_package_uris_are_recognized_but_not_sole_reusable_identity(uri) -> None:
    identity = runtime_env_plan_identity(normalize_runtime_env({"working_dir": uri}))
    generic = runtime_env_plan_identity(
        normalize_runtime_env({"working_dir": "gcs://project/runtime.zip"})
    )

    assert identity.reusable is False
    assert identity.unresolved_paths == ("spec.working_dir",)
    assert identity.retry_safe is True
    assert identity.retry_unsafe_paths == ()
    assert generic.retry_safe is False
    assert generic.retry_unsafe_paths == ("spec.working_dir",)
    assert identity.manifest["digest"] != generic.manifest["digest"]


def test_runtime_env_exclude_patterns_are_never_fingerprinted() -> None:
    first = _materialize(
        step(increment, runtime_env={"excludes": ["credential-low-entropy"]}),
        1,
    ).plan
    rotated = _materialize(
        step(increment, runtime_env={"excludes": ["credential-rotated"]}),
        1,
    ).plan

    assert first.fingerprint == rotated.fingerprint
    assert "credential-low-entropy" not in first.canonical_json
    assert any(
        rejection.code == "UNRESOLVED_RUNTIME_ENV" for rejection in first.eligibility.rejections
    )


def test_arbitrary_ray_metadata_values_are_execution_only() -> None:
    first = _materialize(
        step(
            increment,
            ray_options={
                "name": "credential-low-entropy",
                "_labels": {"tenant": "customer-low-entropy"},
                "label_selector": {"namespace": "namespace-low-entropy"},
            },
        ),
        1,
    ).plan
    rotated = _materialize(
        step(
            increment,
            ray_options={
                "name": "credential-rotated",
                "_labels": {"tenant": "customer-rotated"},
                "label_selector": {"namespace": "namespace-rotated"},
            },
        ),
        1,
    ).plan

    assert first.fingerprint == rotated.fingerprint
    assert "low-entropy" not in first.canonical_json
    assert any(
        rejection.code == "UNRESOLVED_PLAN_OPTION" for rejection in first.eligibility.rejections
    )

    without_annotations = _materialize(
        step(increment, ray_options={"label_selector": {"namespace": "namespace-rotated"}}),
        1,
    ).plan
    assert rotated.fingerprint == without_annotations.fingerprint


def test_scheduling_revision_is_distinct_from_environment_revision() -> None:
    environment_only = _materialize(
        step(increment, ray_options={"label_selector": {"region": "west"}}),
        1,
        context=replace(
            BASE_CONTEXT,
            trust_identity={"environment_revision": "environment-v1"},
        ),
    ).plan
    scheduled_v1 = _materialize(
        step(increment, ray_options={"label_selector": {"region": "west"}}),
        1,
        context=replace(
            BASE_CONTEXT,
            trust_identity={"scheduling_revision": "placement-v1"},
        ),
    ).plan
    scheduled_v2 = _materialize(
        step(increment, ray_options={"label_selector": {"region": "east"}}),
        1,
        context=replace(
            BASE_CONTEXT,
            trust_identity={"scheduling_revision": "placement-v2"},
        ),
    ).plan

    assert any(
        rejection.code == "UNRESOLVED_PLAN_OPTION"
        for rejection in environment_only.eligibility.rejections
    )
    assert not any(
        rejection.code == "UNRESOLVED_PLAN_OPTION"
        for rejection in scheduled_v1.eligibility.rejections
    )
    assert scheduled_v1.fingerprint != scheduled_v2.fingerprint


def test_local_code_identity_uses_ray_working_dir_exclusions(
    monkeypatch,
    tmp_path,
) -> None:
    from ray._private import ray_constants

    monkeypatch.setattr(
        ray_constants,
        "get_runtime_env_default_excludes",
        lambda: ["venv"],
        raising=False,
    )
    source = tmp_path / "code"
    source.mkdir()
    (source / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    virtualenv = source / "venv"
    virtualenv.mkdir()
    ignored = virtualenv / "ignored.py"
    ignored.write_text("VALUE = 1\n", encoding="utf-8")
    mercurial = source / ".hg"
    mercurial.mkdir()
    included = mercurial / "included"
    included.write_text("one\n", encoding="utf-8")
    signature = step(increment, runtime_env={"working_dir": str(source)})

    baseline = _materialize(signature, 1).plan
    ignored.write_text("VALUE = 2\n", encoding="utf-8")
    default_excluded_change = _materialize(signature, 1).plan
    included.write_text("two\n", encoding="utf-8")
    included_change = _materialize(signature, 1).plan

    assert baseline.fingerprint == default_excluded_change.fingerprint
    assert default_excluded_change.fingerprint != included_change.fingerprint
    assert any(
        rejection.code == "UNRESOLVED_RUNTIME_ENV" and "working_dir" in rejection.path
        for rejection in baseline.eligibility.rejections
    )
    assert baseline.retry_safe is True
    assert baseline.retry_unsafe_paths == ()


def test_runtime_env_identity_budget_exhaustion_keeps_dynamic_execution(
    tmp_path,
    monkeypatch,
) -> None:
    import django_ray.workflow_plans as plan_module

    source = tmp_path / "large-code"
    source.mkdir()
    (source / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(plan_module, "MAX_CODE_TREE_BYTES", 1)

    materialized = _materialize(
        step(
            increment,
            runtime_env={
                "working_dir": str(source),
                "excludes": ["*.tmp"],
            },
        ),
        1,
    )

    assert materialized.binding_for_node("0").runtime_env_serialized is not None
    assert materialized.plan.retry_safe is False
    assert materialized.plan.retry_unsafe_paths == (
        "environments.by_node.0.spec.excludes",
        "environments.by_node.0.spec.working_dir",
    )
    assert any(
        rejection.code == "UNRESOLVED_RUNTIME_ENV" and "working_dir" in rejection.path
        for rejection in materialized.plan.eligibility.rejections
    )
    assert step(increment, runtime_env={"working_dir": str(source)}).run(1, use_ray=False) == 2


def test_callable_identity_budget_exhaustion_keeps_dynamic_execution(monkeypatch) -> None:
    import django_ray.workflow_plans as plan_module

    monkeypatch.setattr(plan_module, "MAX_CODE_FILE_BYTES", 1)

    materialized = _materialize(step(increment), 1)

    assert materialized.binding_for_node("0") is not None
    assert any(
        rejection.code == "UNRESOLVED_CODE_IDENTITY"
        for rejection in materialized.plan.eligibility.rejections
    )
    assert step(increment).run(1, use_ray=False) == 2


def test_local_py_module_import_root_name_changes_identity(tmp_path) -> None:
    alpha = tmp_path / "alpha_mod"
    beta = tmp_path / "beta_mod"
    alpha.mkdir()
    beta.mkdir()
    for module in (alpha, beta):
        (module / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")

    alpha_plan = _materialize(
        step(increment, runtime_env={"py_modules": [str(alpha)]}),
        1,
    ).plan
    beta_plan = _materialize(
        step(increment, runtime_env={"py_modules": [str(beta)]}),
        1,
    ).plan

    assert alpha_plan.fingerprint != beta_plan.fingerprint
    assert any(
        rejection.code == "UNRESOLVED_RUNTIME_ENV" and "py_modules" in rejection.path
        for rejection in alpha_plan.eligibility.rejections
    )


def test_supported_ray_task_options_match_the_canonical_subset() -> None:
    plan = _materialize(
        step(
            increment,
            ray_options={
                "fallback_strategy": [{"label_selector": {"region": "west"}}],
                "_labels": {"workflow": "sync"},
                "_generator_backpressure_num_objects": 1,
                "retry_exceptions": [ValueError],
                "placement_group": "default",
                "placement_group_bundle_index": 0,
                "placement_group_capture_child_tasks": True,
            },
        ),
        1,
    )
    assert plan.plan.manifest["nodes"][0]["scheduling"]["fallback_strategy"]
    assert plan.binding_for_node("0").ray_options_dict()["retry_exceptions"] == [ValueError]
    assert any(
        rejection.code == "UNRESOLVED_PLAN_OPTION" for rejection in plan.plan.eligibility.rejections
    )

    for unsupported in (
        {"max_calls": 1},
        {"object_store_memory": 1024},
        {"generator_backpressure_num_objects": 1},
    ):
        with pytest.raises(WorkflowPlanValidationError, match="unsupported fields"):
            _materialize(step(increment, ray_options=unsupported), 1)


def test_workflow_signatures_remain_pickle_and_deepcopy_compatible() -> None:
    signature = step(
        increment,
        mode={"nested": ["a", "b"]},
        ray_options={"resources": {"database": 1}},
        runtime_env={"env_vars": {"MODE": "test"}},
    )

    assert pickle.loads(pickle.dumps(signature)) == signature
    assert deepcopy(signature) == signature


def test_local_runtime_env_mutation_fails_before_leaf_submission(tmp_path, monkeypatch) -> None:
    source = tmp_path / "code"
    source.mkdir()
    module = source / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    materialized = _materialize(
        step(increment, runtime_env={"working_dir": str(source)}),
        1,
    )

    def mutate_during_packaging(runtime_env):
        module.write_text("VALUE = 2\n", encoding="utf-8")
        return runtime_env.spec

    monkeypatch.setattr(
        "django_ray.runtime.runtime_env.prepare_runtime_env_for_ray_core",
        mutate_during_packaging,
    )
    with pytest.raises(WorkflowPlanMismatchError, match="local content changed"):
        prepare_materialized_plan_for_ray(materialized)


def test_repeated_step_runtime_env_identity_and_preparation_are_cached(monkeypatch) -> None:
    import django_ray.workflow_plans as plan_module

    original_identity = plan_module.runtime_env_plan_identity
    identity_calls = 0

    def count_identity(*args, **kwargs):
        nonlocal identity_calls
        identity_calls += 1
        return original_identity(*args, **kwargs)

    monkeypatch.setattr(plan_module, "runtime_env_plan_identity", count_identity)
    runtime_env = {"pip": ["example==1.0 --hash=sha256:" + "a" * 64]}
    materialized = _materialize(
        chain(
            step(increment, runtime_env=runtime_env),
            step(increment, runtime_env=runtime_env),
        ),
        1,
    )
    # One outer identity plus one shared per-step identity.
    assert identity_calls == 2

    preparation_calls = 0

    def count_preparation(resolved):
        nonlocal preparation_calls
        preparation_calls += 1
        return resolved.spec

    monkeypatch.setattr(
        "django_ray.runtime.runtime_env.prepare_runtime_env_for_ray_core",
        count_preparation,
    )
    prepare_materialized_plan_for_ray(materialized)
    assert preparation_calls == 1


def test_plan_and_builder_metadata_are_deeply_immutable() -> None:
    ray_options = {"resources": {"database": 1}}
    runtime_env = {"env_vars": {"MODE": "initial"}}
    signature = step(increment, ray_options=ray_options, runtime_env=runtime_env)
    ray_options["resources"]["database"] = 99
    runtime_env["env_vars"]["MODE"] = "mutated"

    materialized = _materialize(signature, 1)
    node = materialized.plan.manifest["nodes"][0]

    assert node["resources"]["custom"]["database"] == 1
    with pytest.raises(TypeError):
        node["resources"]["custom"]["database"] = 2
    with pytest.raises(TypeError):
        signature.ray_options["resources"]["database"] = 2
    assert "mutated" not in (materialized.binding_for_node("0").runtime_env_serialized or "")


def test_process_local_scheduling_objects_are_rejected_before_submission() -> None:
    with pytest.raises(WorkflowPlanValidationError, match="installed Ray task API"):
        _materialize(
            step(increment, ray_options={"scheduling_strategy": object()}),
            1,
        )

    with pytest.raises(WorkflowPlanValidationError, match="unsupported fields") as error:
        _materialize(
            step(increment, ray_options={"metadata": {"token": "do-not-log"}}),
            1,
        )
    assert "do-not-log" not in str(error.value)

    with pytest.raises(WorkflowPlanValidationError, match="num_cpus"):
        _materialize(step(increment, ray_options={"num_cpus": True}), 1)


def test_node_overflow_uses_bounded_dynamic_fallback_and_still_runs_locally() -> None:
    oversized = chain(*(step(increment) for _ in range(MAX_PLAN_NODES + 1)))
    materialized = _materialize(oversized, 1)

    assert materialized.plan.manifest["snapshot"]["reasons"] == ("node_limit",)
    assert materialized.plan.summary()["node_count"] == MAX_PLAN_NODES + 1
    assert len(materialized.plan.canonical_json.encode("utf-8")) <= MAX_PLAN_BYTES
    assert len(materialized.step_bindings) == MAX_PLAN_NODES + 1
    assert materialized.plan.eligibility.eligible_strategies == ("dynamic_tasks", "local")
    assert any(
        rejection.code == "PLAN_SNAPSHOT_OVERFLOW" and rejection.strategy == "compiled_graph"
        for rejection in materialized.plan.eligibility.rejections
    )
    assert oversized.run(0, use_ray=False) == MAX_PLAN_NODES + 1


def test_overflow_fingerprint_covers_omitted_semantic_definition() -> None:
    def overflow_chain(*, final_step):
        return chain(
            *(step(increment) for _ in range(MAX_PLAN_NODES)),
            final_step,
        )

    baseline = _materialize(overflow_chain(final_step=step(increment)), 1).plan
    topology_change = _materialize(
        chain(*(step(increment) for _ in range(MAX_PLAN_NODES + 2))),
        1,
    ).plan
    callable_change = _materialize(overflow_chain(final_step=step(double)), 1).plan
    option_change = _materialize(
        overflow_chain(final_step=step(increment, ray_options={"num_cpus": 2})),
        1,
    ).plan
    environment_change = _materialize(
        overflow_chain(
            final_step=step(increment, runtime_env={"pip": ["example==2.0"]}),
        ),
        1,
    ).plan

    assert environment_change.retry_safe is False
    assert environment_change.manifest["retry_safety"]["total_retry_unsafe_paths"] == 1
    assert environment_change.retry_unsafe_paths == (
        f"environments.by_node.0.{MAX_PLAN_NODES}.spec.pip.0",
    )

    assert (
        len(
            {
                baseline.fingerprint,
                topology_change.fingerprint,
                callable_change.fingerprint,
                option_change.fingerprint,
                environment_change.fingerprint,
            }
        )
        == 5
    )


def test_overflow_fingerprint_normalizes_unicode_before_hashing() -> None:
    oversized = chain(*(step(increment) for _ in range(MAX_PLAN_NODES + 1)))
    composed = _materialize(
        oversized,
        1,
        context=replace(
            BASE_CONTEXT, build_revision="build:caf\N{LATIN SMALL LETTER E WITH ACUTE}"
        ),
    ).plan
    decomposed = _materialize(
        oversized,
        1,
        context=replace(BASE_CONTEXT, build_revision="build:cafe\N{COMBINING ACUTE ACCENT}"),
    ).plan

    assert composed.canonical_json == decomposed.canonical_json
    assert composed.fingerprint == decomposed.fingerprint


def test_byte_overflow_uses_bounded_dynamic_fallback() -> None:
    bound_schema = {f"argument_{index:03d}_{'x' * 32}": index for index in range(160)}
    signature = chain(*(step(increment, **bound_schema) for _ in range(16)))

    materialized = _materialize(signature, 1)

    assert materialized.plan.manifest["snapshot"]["reasons"] == ("byte_limit",)
    assert materialized.plan.manifest["snapshot"]["observed_canonical_bytes"] > MAX_PLAN_BYTES
    assert len(materialized.plan.canonical_json.encode("utf-8")) <= MAX_PLAN_BYTES
    assert len(materialized.step_bindings) == 16


def test_many_result_buffers_use_bounded_physical_topology_overflow() -> None:
    def signature(memory: int):
        return chain(
            *(
                map_step(increment)
                .with_limits(max_items=2, max_concurrency=1)
                .with_result_buffer(
                    max_serialized_bytes=1024,
                    actor_options={"num_cpus": 0.1, "memory": memory},
                )
                for _ in range(55)
            )
        )

    baseline = _materialize(signature(2048), [1]).plan
    changed = _materialize(signature(4096), [1]).plan
    manifest = baseline.as_dict()
    physical = manifest["physical_topology"]

    assert manifest["snapshot"]["state"] == "overflow"
    assert len(baseline.canonical_json.encode("utf-8")) <= MAX_PLAN_BYTES
    assert physical["actors"] == []
    assert physical["placement_relationships"] == []
    assert physical["overflow_summary"] == {
        "stage_count": 0,
        "actor_count": 55,
        "result_buffer_actor_count": 55,
        "placement_relationship_count": 55,
        "details_omitted": True,
    }
    assert changed.fingerprint != baseline.fingerprint
    assert changed.manifest["snapshot"]["source_digest"] != manifest["snapshot"]["source_digest"]


def test_long_custom_result_buffer_resources_use_bounded_overflow() -> None:
    def signature(resource_value: int):
        resources = {f"{'r' * 250}{index:06d}": resource_value for index in range(32)}
        buffered = [
            map_step(increment)
            .with_limits(max_items=2, max_concurrency=1)
            .with_result_buffer(
                max_serialized_bytes=1024,
                actor_options={
                    "num_cpus": 0.1,
                    "memory": 2048,
                    "resources": resources,
                },
            )
            for _ in range(2)
        ]
        return chain(*buffered)

    baseline = _materialize(signature(1), [1]).plan
    changed = _materialize(signature(2), [1]).plan
    manifest = baseline.as_dict()

    assert manifest["snapshot"]["state"] == "overflow"
    assert manifest["snapshot"]["reasons"] == ["byte_limit"]
    assert len(baseline.canonical_json.encode("utf-8")) <= MAX_PLAN_BYTES
    assert manifest["physical_topology"]["overflow_summary"] == {
        "stage_count": 0,
        "actor_count": 2,
        "result_buffer_actor_count": 2,
        "placement_relationship_count": 2,
        "details_omitted": True,
    }
    assert changed.fingerprint != baseline.fingerprint
    assert changed.manifest["snapshot"]["source_digest"] != manifest["snapshot"]["source_digest"]


def test_deep_definition_materializes_without_recursion_error() -> None:
    signature = step(increment)
    for _ in range(sys.getrecursionlimit() + 100):
        signature = chain(signature)

    materialized = _materialize(signature, 1)

    assert "identifier_limit" in materialized.plan.manifest["snapshot"]["reasons"]
    assert any(
        rejection.code == "PLAN_SNAPSHOT_OVERFLOW"
        for rejection in materialized.plan.eligibility.rejections
    )


def test_moderate_repeated_callable_chain_fits_the_plan_bound() -> None:
    materialized = _materialize(
        chain(*(step(increment) for _ in range(50))),
        1,
    )

    assert len(materialized.plan.canonical_json.encode("utf-8")) < 64 * 1024
    assert len(materialized.plan.manifest["callables"]) == 1


def test_cache_route_and_drain_use_the_exact_fingerprint() -> None:
    first = _materialize(step(increment), 1).plan
    replacement = _materialize(step(increment, ray_options={"num_cpus": 2}), 1).plan

    assert first.cache_key("compiled_graph").endswith(first.fingerprint)
    assert plan_requires_drain(first.fingerprint, first) is False
    assert plan_requires_drain(first.fingerprint, replacement) is True
    first.assert_owner_fingerprint(first.fingerprint)
    with pytest.raises(WorkflowPlanMismatchError, match="must drain"):
        replacement.assert_owner_fingerprint(first.fingerprint)


@pytest.mark.django_db
def test_plan_is_pinned_and_observable_without_progress() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="workflow-plan-local",
        callable_path="tests.unit.test_workflow_plans.increment",
        state=TaskState.RUNNING,
        execution_generation=1,
    )

    with durable_task_execution(
        execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
    ):
        assert step(increment).run(1, use_ray=False) == 2

    execution.refresh_from_db()
    assert execution.progress_data is None
    assert execution.workflow_plan_fingerprint.startswith("sha256:")
    summary = get_task_summary(execution)
    snapshot = get_workflow_plan(execution)
    assert summary["workflow_selected_strategy"] == "local"
    assert summary["workflow_reporting_policy"] == "disabled"
    assert snapshot is not None
    assert snapshot["fingerprint"] == execution.workflow_plan_fingerprint
    assert "secret" not in execution.workflow_plan_json.lower()


@pytest.mark.django_db
def test_stale_local_fence_aborts_before_application_side_effects() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="workflow-plan-stale-local",
        callable_path=f"{__name__}.record_side_effect",
        state=TaskState.RUNNING,
        execution_generation=7,
    )
    SIDE_EFFECTS.clear()

    with durable_task_execution(
        execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=6,
    ):
        with pytest.raises(WorkflowPlanMismatchError, match="stale"):
            step(record_side_effect).run(3, use_ray=False)

    assert SIDE_EFFECTS == []
    execution.refresh_from_db()
    assert execution.workflow_plan_fingerprint is None


@pytest.mark.django_db
def test_stale_ray_fence_aborts_before_preparation_or_submission(monkeypatch) -> None:
    from django_ray.workflows import _RayExecutor

    execution = RayTaskExecution.objects.create(
        task_id="workflow-plan-stale-ray",
        callable_path=f"{__name__}.increment",
        state=TaskState.RUNNING,
        execution_generation=5,
    )
    executor = object.__new__(_RayExecutor)
    executor.task_context = DurableTaskContext(
        task_pk=execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=4,
    )
    executor.progress_actor_cls = object()
    prepared = False

    def record_preparation(materialized_plan):
        nonlocal prepared
        prepared = True
        return materialized_plan

    monkeypatch.setattr(
        "django_ray.workflow_plans.prepare_materialized_plan_for_ray",
        record_preparation,
    )

    with pytest.raises(WorkflowPlanMismatchError, match="stale"):
        executor.bind_plan(_materialize(step(increment), 1), requested_policy="auto")

    assert prepared is False
    assert not hasattr(executor, "workflow_run_identity")


@pytest.mark.django_db
def test_ray_run_rechecks_fence_after_preparation_before_actor_or_leaf(monkeypatch) -> None:
    from django_ray.workflows import _RayExecutor

    execution = RayTaskExecution.objects.create(
        task_id="workflow-plan-stale-during-preparation",
        callable_path=f"{__name__}.increment",
        state=TaskState.RUNNING,
        execution_generation=6,
    )
    materialized = _materialize(step(increment), 1)
    executor = object.__new__(_RayExecutor)
    executor.task_context = DurableTaskContext(
        task_pk=execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
    )
    actor_creations = 0

    class ProgressActor:
        @staticmethod
        def remote(*args):
            nonlocal actor_creations
            actor_creations += 1
            return object()

    executor.progress_actor_cls = ProgressActor()

    def cancel_during_preparation(materialized_plan):
        RayTaskExecution.objects.filter(pk=execution.pk).update(state=TaskState.CANCELLED)
        return materialized_plan

    monkeypatch.setattr(
        "django_ray.workflow_plans.prepare_materialized_plan_for_ray",
        cancel_during_preparation,
    )

    with pytest.raises(WorkflowPlanMismatchError, match="stale during RuntimeEnv preparation"):
        executor.bind_plan(materialized, requested_policy="auto")

    assert actor_creations == 0
    assert not hasattr(executor, "workflow_run_identity")


@pytest.mark.django_db
def test_terminal_only_preparation_failure_keeps_a_plan_for_durable_failure_summary(
    monkeypatch,
) -> None:
    from django_ray.workflows import _RayExecutor

    execution = RayTaskExecution.objects.create(
        task_id="workflow-terminal-only-preparation-failure",
        callable_path=f"{__name__}.increment",
        state=TaskState.RUNNING,
        execution_generation=7,
    )
    materialized = _materialize(step(increment), 1)
    executor = object.__new__(_RayExecutor)
    executor.task_context = DurableTaskContext(
        task_pk=execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
    )
    executor.progress_actor_cls = None

    def fail_preparation(_materialized_plan):
        raise RuntimeError("RuntimeEnv packaging failed")

    monkeypatch.setattr(
        "django_ray.workflow_plans.prepare_materialized_plan_for_ray",
        fail_preparation,
    )

    with pytest.raises(RuntimeError, match="RuntimeEnv packaging failed"):
        executor.bind_plan(
            materialized,
            requested_policy="auto",
            reporting_policy="terminal_only",
        )

    assert not hasattr(executor, "workflow_run_identity")
    execution.refresh_from_db()
    assert execution.workflow_run_id is not None
    assert execution.workflow_plan_json == materialized.plan.canonical_json
    assert execution.workflow_progress_summary_json is None

    assert record_failure(
        execution,
        error_message="RuntimeEnv preparation failed",
        retry=False,
    )

    execution.refresh_from_db()
    assert execution.workflow_progress_summary_json is not None
    summary = deserialize_workflow_progress_summary(execution.workflow_progress_summary_json)
    assert execution.state == TaskState.FAILED
    assert summary["state"] == TaskState.FAILED
    assert summary["reporting_policy"] == "terminal_only"
    assert summary["node_counts"]["declared"] == len(materialized.plan.manifest["nodes"])
    assert summary["edge_counts"]["declared"] == len(materialized.plan.manifest["edges"])
    assert execution.progress_data is None


@pytest.mark.django_db
def test_terminal_only_result_serialization_failure_publishes_only_failed_summary(
    monkeypatch,
) -> None:
    from django_ray.runtime import entrypoint

    execution = RayTaskExecution.objects.create(
        task_id="workflow-terminal-only-result-serialization-failure",
        callable_path="tests.return_unserializable_workflow_result",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=8,
    )
    materialized = _materialize(step(increment), 1)

    def return_unserializable_result() -> set[object]:
        context = get_current_task_context()
        assert context is not None
        selection = materialized.plan.eligibility.select(
            "dynamic_tasks",
            requested_policy="auto",
            reporting_policy="terminal_only",
        )
        identity = allocate_workflow_run(
            context,
            plan=materialized.plan,
            selection=selection,
        )
        assert identity is not None
        return {object()}

    monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)
    monkeypatch.setattr(
        "django_ray.runtime.import_utils.import_callable",
        lambda _path: return_unserializable_result,
    )

    result_json = entrypoint.execute_task(
        execution.callable_path,
        "[]",
        "{}",
        task_execution_pk=execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
    )
    result = json.loads(result_json)

    execution.refresh_from_db()
    assert result["success"] is False
    assert result["exception_type"] == "builtins.TypeError"
    assert execution.state == TaskState.RUNNING
    assert execution.workflow_progress_summary_json is None
    assert execution.completion_data == result_json

    assert record_failure(
        execution,
        error_message=result["error"],
        error_traceback=result["traceback"],
        retry=False,
        expected_attempt_number=1,
        expected_execution_generation=8,
        expected_completion_data=result_json,
        require_completion_data_match=True,
    )

    execution.refresh_from_db()
    assert execution.workflow_progress_summary_json is not None
    summary = deserialize_workflow_progress_summary(execution.workflow_progress_summary_json)
    assert execution.state == TaskState.FAILED
    assert summary["state"] == TaskState.FAILED
    assert summary["summary_revision"] == 1
    assert summary["terminal"]["outcome"] == TaskState.FAILED
    assert summary["node_counts"]["declared"] == len(materialized.plan.manifest["nodes"])
    assert execution.progress_data is None


@pytest.mark.django_db
def test_ray_bind_plan_creates_actor_from_one_fenced_initialized_event() -> None:
    from django_ray.workflows import _RayExecutor

    execution = RayTaskExecution.objects.create(
        task_id="workflow-progress-initialized",
        callable_path=f"{__name__}.increment",
        state=TaskState.RUNNING,
        execution_generation=2,
    )
    materialized = _materialize(step(increment), 1)
    executor = object.__new__(_RayExecutor)
    executor.task_context = DurableTaskContext(
        task_pk=execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
    )
    executor.progress_actor = None
    actor = object()
    actor_inputs: list[bytes] = []

    class ProgressActor:
        @staticmethod
        def remote(initialized_event: bytes):
            actor_inputs.append(initialized_event)
            return actor

    executor.progress_actor_cls = ProgressActor()

    executor.bind_plan(
        materialized,
        requested_policy="auto",
        reporting_policy="full",
    )

    assert executor.progress_actor is actor
    assert executor.workflow_run_identity is not None
    assert len(actor_inputs) == 1
    event = decode_workflow_progress_event(
        actor_inputs[0],
        expected_run_identity=executor.workflow_run_identity.as_dict(),
    )
    assert event.kind is WorkflowProgressEventKind.INITIALIZED
    assert event.payload == {"plan": materialized.plan.summary()}


@pytest.mark.django_db
def test_disabled_ray_reporting_pins_policy_without_actor_or_codec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from django_ray.workflows import _RayExecutor

    execution = RayTaskExecution.objects.create(
        task_id="workflow-progress-disabled",
        callable_path=f"{__name__}.increment",
        state=TaskState.RUNNING,
        execution_generation=2,
    )
    materialized = _materialize(step(increment), 1)
    executor = object.__new__(_RayExecutor)
    executor.task_context = DurableTaskContext(
        task_pk=execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
    )
    executor.progress_actor = None
    actor_creations = 0

    class ProgressActor:
        @staticmethod
        def remote(*args):
            del args
            nonlocal actor_creations
            actor_creations += 1
            return object()

    executor.progress_actor_cls = ProgressActor()
    prepared_events: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        "django_ray.workflow_progress_protocol.prepare_workflow_progress_event",
        lambda *args, **kwargs: prepared_events.append((*args, kwargs)),
    )

    executor.bind_plan(
        materialized,
        requested_policy="auto",
        reporting_policy="disabled",
    )

    execution.refresh_from_db()
    selection = json.loads(execution.workflow_plan_selection)
    assert actor_creations == 0
    assert prepared_events == []
    assert executor.progress_actor is None
    assert executor.workflow_run_identity is not None
    assert selection["reporting_policy"] == "disabled"
    assert execution.progress_data is None


@pytest.mark.django_db
def test_plan_pinning_enforces_serialized_plan_and_selection_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = RayTaskExecution.objects.create(
        task_id="workflow-plan-persistence-bounds",
        callable_path=f"{__name__}.increment",
        state=TaskState.RUNNING,
        execution_generation=2,
    )
    plan = _materialize(step(increment), 1).plan
    selection = plan.eligibility.select("dynamic_tasks", requested_policy="auto")
    oversized_plan = replace(plan, canonical_json="x" * (MAX_PLAN_BYTES + 1))

    with pytest.raises(ValueError, match="workflow plan exceeds persistence limit"):
        allocate_workflow_run(
            _task_context(execution),
            plan=oversized_plan,
            selection=selection,
        )

    monkeypatch.setattr("django_ray.workflow_progress.MAX_PLAN_SELECTION_BYTES", 1)
    with pytest.raises(ValueError, match="selection exceeds persistence limit"):
        allocate_workflow_run(
            _task_context(execution),
            plan=plan,
            selection=selection,
        )


@pytest.mark.django_db
def test_plan_pinning_rejects_same_fingerprint_with_different_manifest() -> None:
    plan = _materialize(step(increment), 1).plan
    selection = plan.eligibility.select("dynamic_tasks", requested_policy="auto")
    execution = RayTaskExecution.objects.create(
        task_id="workflow-plan-manifest-mismatch",
        callable_path=f"{__name__}.increment",
        state=TaskState.RUNNING,
        execution_generation=2,
        workflow_plan_fingerprint=plan.fingerprint,
        workflow_plan_json="{}",
    )
    with pytest.raises(WorkflowPlanMismatchError, match="manifest does not match"):
        allocate_workflow_run(
            _task_context(execution),
            plan=plan,
            selection=selection,
        )


@pytest.mark.django_db
def test_retry_unsafe_plan_message_reports_truncated_path_count() -> None:
    plan = _materialize(step(increment), 1).plan
    manifest = plan.as_dict()
    manifest["retry_safety"] = {
        "retry_safe": False,
        "retry_unsafe_paths": [f"environments.by_node.{index}" for index in range(7)],
        "total_retry_unsafe_paths": 7,
        "retry_unsafe_paths_truncated": False,
    }
    unsafe_plan = replace(plan, manifest=manifest)
    selection = plan.eligibility.select("dynamic_tasks", requested_policy="auto")
    execution = RayTaskExecution.objects.create(
        task_id="workflow-plan-truncated-retry-diagnostics",
        callable_path=f"{__name__}.increment",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=2,
        workflow_plan_fingerprint=plan.fingerprint,
        workflow_plan_json=plan.canonical_json,
        workflow_plan_pinned_attempt=1,
    )
    with pytest.raises(WorkflowPlanMismatchError, match="and 2 more"):
        allocate_workflow_run(
            _task_context(execution),
            plan=unsafe_plan,
            selection=selection,
        )


@pytest.mark.django_db
def test_retry_must_match_the_plan_pinned_by_the_first_attempt() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="workflow-plan-retry",
        callable_path="tests.unit.test_workflow_plans.increment",
        state=TaskState.RUNNING,
        execution_generation=3,
    )
    first_plan = _materialize(step(increment), 1).plan
    first_selection = first_plan.eligibility.select("dynamic_tasks", requested_policy="auto")
    _allocate(execution, first_plan, first_selection)
    assert record_failure(execution, error_message="retry", retry=True)
    RayTaskExecution.objects.filter(pk=execution.pk).update(state=TaskState.RUNNING)
    execution.refresh_from_db()

    replacement = _materialize(step(increment, ray_options={"num_cpus": 2}), 1).plan
    replacement_selection = replacement.eligibility.select("dynamic_tasks", requested_policy="auto")
    with pytest.raises(WorkflowPlanMismatchError, match="different effective plan"):
        allocate_workflow_run(
            _task_context(execution),
            plan=replacement,
            selection=replacement_selection,
        )

    execution.refresh_from_db()
    assert execution.workflow_plan_fingerprint == first_plan.fingerprint
    assert execution.workflow_plan_pinned_attempt == 1
    assert execution.workflow_run_id is None


@pytest.mark.django_db
def test_retry_rejects_result_buffer_resource_drift_before_effects() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="workflow-result-buffer-plan-retry",
        callable_path=f"{__name__}.record_side_effect",
        state=TaskState.RUNNING,
        execution_generation=3,
    )

    def buffered(memory: int):
        return (
            map_step(record_side_effect)
            .with_limits(
                max_items=4,
                max_concurrency=2,
            )
            .with_result_buffer(
                max_serialized_bytes=4096,
                actor_options={"num_cpus": 0.25, "memory": memory},
            )
        )

    first_plan = _materialize(buffered(8192), [1]).plan
    first_selection = first_plan.eligibility.select(
        "dynamic_tasks",
        requested_policy="auto",
    )
    _allocate(execution, first_plan, first_selection)
    assert record_failure(execution, error_message="retry", retry=True)
    RayTaskExecution.objects.filter(pk=execution.pk).update(state=TaskState.RUNNING)
    execution.refresh_from_db()

    replacement = _materialize(buffered(16384), [1]).plan
    replacement_selection = replacement.eligibility.select(
        "dynamic_tasks",
        requested_policy="auto",
    )
    SIDE_EFFECTS.clear()

    with pytest.raises(WorkflowPlanMismatchError, match="different effective plan"):
        allocate_workflow_run(
            _task_context(execution),
            plan=replacement,
            selection=replacement_selection,
        )

    assert SIDE_EFFECTS == []


@pytest.mark.django_db
def test_retry_rejects_opaque_runtime_env_even_when_secret_free_plan_matches() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="workflow-plan-opaque-runtime-retry",
        callable_path=f"{__name__}.increment",
        state=TaskState.RUNNING,
        execution_generation=4,
    )
    first_plan = _materialize(
        step(increment, runtime_env={"working_dir": "https://first.invalid/code.zip"}),
        1,
    ).plan
    rotated_plan = _materialize(
        step(increment, runtime_env={"working_dir": "https://second.invalid/code.zip"}),
        1,
    ).plan
    assert first_plan.fingerprint == rotated_plan.fingerprint
    assert first_plan.retry_safe is False
    assert first_plan.retry_unsafe_paths == ("environments.by_node.0.spec.working_dir",)
    selection = first_plan.eligibility.select("dynamic_tasks", requested_policy="auto")
    _allocate(execution, first_plan, selection)

    # Repeated binding in the same fenced attempt is allowed. A later durable
    # attempt is not, because the redacted URI cannot be compared to the pin.
    _allocate(execution, rotated_plan, selection)
    assert record_failure(execution, error_message="retry", retry=True)
    RayTaskExecution.objects.filter(pk=execution.pk).update(state=TaskState.RUNNING)
    execution.refresh_from_db()
    with pytest.raises(
        WorkflowPlanMismatchError,
        match="cannot verify runtime environment",
    ) as error:
        allocate_workflow_run(
            _task_context(execution),
            plan=rotated_plan,
            selection=selection,
        )
    assert "first.invalid" not in str(error.value)
    assert "second.invalid" not in str(error.value)

    execution.refresh_from_db()
    assert execution.workflow_plan_pinned_attempt == 1
    assert execution.workflow_run_id is None


@pytest.mark.django_db
def test_retry_allows_content_hashed_local_runtime_env(tmp_path) -> None:
    source = tmp_path / "retry-safe-code"
    source.mkdir()
    (source / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    ignored = source / "ignored.tmp"
    ignored.write_text("not packaged\n", encoding="utf-8")
    plan = _materialize(
        step(
            increment,
            runtime_env={
                "working_dir": str(source),
                "excludes": ["*.tmp"],
            },
        ),
        1,
    ).plan
    assert plan.retry_safe is True
    assert any(
        rejection.code == "UNRESOLVED_RUNTIME_ENV" for rejection in plan.eligibility.rejections
    )
    selection = plan.eligibility.select("dynamic_tasks", requested_policy="auto")
    execution = RayTaskExecution.objects.create(
        task_id="workflow-plan-local-runtime-retry",
        callable_path=f"{__name__}.increment",
        state=TaskState.RUNNING,
        execution_generation=5,
    )
    _allocate(execution, plan, selection)
    assert record_failure(execution, error_message="retry", retry=True)
    RayTaskExecution.objects.filter(pk=execution.pk).update(state=TaskState.RUNNING)
    execution.refresh_from_db()
    _allocate(execution, plan, selection)
    execution.refresh_from_db()
    assert execution.workflow_plan_pinned_attempt == 1


def test_explicit_excludes_do_not_make_an_opaque_code_uri_retry_safe() -> None:
    identity = runtime_env_plan_identity(
        normalize_runtime_env(
            {
                "working_dir": "https://example.invalid/code.zip",
                "excludes": ["*.tmp"],
            }
        )
    )

    assert identity.retry_safe is False
    assert identity.retry_unsafe_paths == ("spec.excludes", "spec.working_dir")


@pytest.mark.django_db
def test_rolling_writer_pin_attempt_is_initialized_only_for_retry_safe_plan() -> None:
    safe_plan = _materialize(step(increment), 1).plan
    safe_selection = safe_plan.eligibility.select("dynamic_tasks", requested_policy="auto")
    safe_execution = RayTaskExecution.objects.create(
        task_id="workflow-plan-rolling-safe",
        callable_path=f"{__name__}.increment",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=6,
        workflow_plan_fingerprint=safe_plan.fingerprint,
        workflow_plan_json=safe_plan.canonical_json,
    )
    _allocate(safe_execution, safe_plan, safe_selection)
    safe_execution.refresh_from_db()
    assert safe_execution.workflow_plan_pinned_attempt == 2

    unsafe_plan = _materialize(
        step(increment, runtime_env={"pip": ["mutable-package==1.0"]}),
        1,
    ).plan
    unsafe_selection = unsafe_plan.eligibility.select("dynamic_tasks", requested_policy="auto")
    unsafe_execution = RayTaskExecution.objects.create(
        task_id="workflow-plan-rolling-unsafe",
        callable_path=f"{__name__}.increment",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=7,
        workflow_plan_fingerprint=unsafe_plan.fingerprint,
        workflow_plan_json=unsafe_plan.canonical_json,
    )
    with pytest.raises(WorkflowPlanMismatchError, match="cannot verify runtime environment"):
        allocate_workflow_run(
            _task_context(unsafe_execution),
            plan=unsafe_plan,
            selection=unsafe_selection,
        )

    unsafe_execution.refresh_from_db()
    assert unsafe_execution.workflow_plan_pinned_attempt is None
    assert unsafe_execution.workflow_run_id is None


@pytest.mark.django_db
def test_observability_detects_plan_snapshot_tampering() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="workflow-plan-tampered",
        callable_path="tests.unit.test_workflow_plans.increment",
        workflow_plan_fingerprint="sha256:" + "0" * 64,
        workflow_plan_json='{"changed":true}',
    )

    with pytest.raises(RuntimeError, match="fingerprint does not match"):
        get_workflow_plan(execution)


@pytest.mark.django_db
def test_observability_rejects_unversioned_plan_selection() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="workflow-plan-selection-tampered",
        callable_path=f"{__name__}.increment",
        state=TaskState.RUNNING,
        execution_generation=1,
    )
    with durable_task_execution(
        execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
    ):
        assert step(increment).run(1, use_ray=False) == 2
    RayTaskExecution.objects.filter(pk=execution.pk).update(
        workflow_plan_selection='{"selected_strategy":"local"}'
    )
    execution.refresh_from_db()

    with pytest.raises(RuntimeError, match="invalid schema"):
        get_workflow_plan(execution)


def test_workflow_plan_fingerprint_is_nullable_for_rolling_writers() -> None:
    field = RayTaskExecution._meta.get_field("workflow_plan_fingerprint")
    assert field.null is True
    assert RayTaskExecution._meta.get_field("workflow_plan_pinned_attempt").null is True
