"""Unit tests for the whole-plan strategy seam (#70) and default-off Compiled Graph strategy (#72)."""

from __future__ import annotations

from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured

from django_ray.runtime.compiled_graph import (
    COMPILED_GRAPH_POLICY_VERSION,
    CompiledGraphCapabilityDecision,
    CompiledGraphReason,
    CompiledGraphRuntimeIdentity,
    CompiledGraphSubmissionTransport,
    CompiledGraphTopology,
    CompiledGraphTransport,
)
from django_ray.runtime.compiled_graph_lifecycle import (
    CompiledGraphInvocationState,
    CompiledGraphSessionState,
    LifecycleEffectState,
    LifecycleOutcome,
)
from django_ray.runtime.context import (
    WorkflowRunIdentity,
    durable_task_execution,
)
from django_ray.workflow.plans import (
    WorkflowPlanBuildContext,
    materialize_workflow_plan,
)
from django_ray.workflow.strategy import (
    CompiledGraphExecutionStrategy,
    CompiledGraphStrategyHandle,
    LocalStrategyHandle,
    LocalWorkflowExecutionStrategy,
    StaleWorkflowRunCallbackError,
    StaticActorPoolSpec,
    WorkflowExecutionStrategyName,
    WorkflowRegionSpec,
    WorkflowRequestedStrategyPolicy,
    WorkflowStrategyFallbackError,
    WorkflowStrategySelectionError,
    attempt_strategy_fallback,
    build_fixed_actor_graph_plan,
    compiled_graph_experimental_enabled,
    resolve_execution_strategy,
    verify_run_callback_identity,
)
from django_ray.workflows import chain, group, map_step, step


def _add_one(value: int) -> int:
    return value + 1


def _multiply(value: int, factor: int = 2) -> int:
    return value * factor


def _sum_pair(values: list[int]) -> int:
    return sum(values)


def _boom(_x: int) -> int:
    raise ValueError("step exploded")


async def _async_leaf(value: int) -> int:
    return value + 1


def _verified_capability_decision() -> CompiledGraphCapabilityDecision:
    return CompiledGraphCapabilityDecision(
        eligible=True,
        verified=True,
        reason=CompiledGraphReason.ELIGIBLE,
        message="Hermetic unit-test capability decision",
        topology=CompiledGraphTopology.RAY_JOB_DRIVER.value,
        submission_transport=CompiledGraphSubmissionTransport.RAY_JOB.value,
        transport=CompiledGraphTransport.CPU_SHARED_MEMORY.value,
        runtime=CompiledGraphRuntimeIdentity(
            ray_version="2.54.1",
            python_version="3.12",
            operating_system="Linux",
            architecture="x86_64",
            deployment_profile="kuberay-2.54.1-py312",
            dependency_profile="cgraph-v1",
        ),
        policy_version=COMPILED_GRAPH_POLICY_VERSION,
        capability_set="test-verified-cgraph",
    )


def _clean_build_context() -> WorkflowPlanBuildContext:
    return WorkflowPlanBuildContext(
        build_revision="git:0123456789abcdef0123456789abcdef01234567",
        container_image_digest="sha256:" + ("a" * 64),
        trust_identity={"trust_domain": "unit-test"},
        compiled_graph_runtime=CompiledGraphRuntimeIdentity(
            ray_version="2.54.1",
            python_version="3.12",
            operating_system="Linux",
            architecture="x86_64",
            deployment_profile="sha256:" + ("a" * 64),
            dependency_profile="cgraph-v1",
        ),
        compiled_graph_topology=CompiledGraphTopology.RAY_JOB_DRIVER,
        compiled_graph_submission_transport=CompiledGraphSubmissionTransport.RAY_JOB,
    )


def test_local_strategy_lifecycle_and_opaque_handle(settings) -> None:
    settings.DJANGO_RAY = {
        **getattr(settings, "DJANGO_RAY", {}),
        "WORKFLOW_PLAN_CODE_REVISION": "rev-1",
    }
    wf = chain(step(_add_one), step(_multiply, factor=3))
    plan = materialize_workflow_plan(wf, invocation_args=(4,), build_context=_clean_build_context())
    strategy = LocalWorkflowExecutionStrategy()
    run_id = WorkflowRunIdentity(
        task_execution_pk=42,
        attempt_number=1,
        execution_generation=1,
        run_id="run-42",
    )

    selection = strategy.validate(plan, requested_policy="local", reporting_policy="disabled")
    assert selection.selected_strategy == "local"
    assert selection.reporting_policy == "disabled"

    prepared = strategy.prepare(plan, selection=selection, run_identity=run_id)
    admitted = strategy.admit(prepared)
    handle = strategy.execute(admitted, wf, (4,), {})

    assert isinstance(handle, LocalStrategyHandle)
    assert handle.strategy_name == "local"
    assert handle.plan_fingerprint == plan.plan.fingerprint
    assert handle.opaque_token.startswith("local-handle:")
    assert not hasattr(handle, "object_ref")

    assert strategy.consume_result(handle, expected_run_identity=run_id) == 15
    with pytest.raises(RuntimeError, match="already been consumed"):
        strategy.consume_result(handle, expected_run_identity=run_id)

    strategy.drain(handle, expected_run_identity=run_id)
    strategy.cleanup(handle, expected_run_identity=run_id)
    assert handle._drained is True
    assert handle._cleaned_up is True


def test_stale_run_callback_fencing_rejects_mismatched_attempt() -> None:
    active_identity = WorkflowRunIdentity(
        task_execution_pk=10,
        attempt_number=2,
        execution_generation=3,
        run_id="run-10-a2",
    )
    stale_identity = WorkflowRunIdentity(
        task_execution_pk=10,
        attempt_number=1,
        execution_generation=2,
        run_id="run-10-a1",
    )
    verify_run_callback_identity(active_identity, active_identity)
    with pytest.raises(StaleWorkflowRunCallbackError, match="Stale workflow run callback"):
        verify_run_callback_identity(active_identity, stale_identity)


def test_pre_start_vs_post_start_strategy_fallback(settings) -> None:
    settings.DJANGO_RAY = {
        **getattr(settings, "DJANGO_RAY", {}),
        "WORKFLOW_PLAN_CODE_REVISION": "rev-1",
    }
    wf = step(_add_one)
    plan = materialize_workflow_plan(wf, invocation_args=(1,), build_context=_clean_build_context())

    selection = attempt_strategy_fallback(
        requested_policy="auto",
        from_strategy="dynamic_tasks",
        to_strategy="local",
        effect_state=LifecycleEffectState.NOT_STARTED,
        eligibility=plan.plan.eligibility,
        reporting_policy="disabled",
    )
    assert selection.selected_strategy == "local"

    with pytest.raises(WorkflowStrategyFallbackError, match="side effects may have started"):
        attempt_strategy_fallback(
            requested_policy="auto",
            from_strategy="dynamic_tasks",
            to_strategy="local",
            effect_state=LifecycleEffectState.MAY_HAVE_STARTED,
            eligibility=plan.plan.eligibility,
            reporting_policy="disabled",
        )

    with pytest.raises(WorkflowStrategyFallbackError, match="prohibits fallback"):
        attempt_strategy_fallback(
            requested_policy="dynamic_tasks",
            from_strategy="dynamic_tasks",
            to_strategy="local",
            effect_state=LifecycleEffectState.NOT_STARTED,
            eligibility=plan.plan.eligibility,
            reporting_policy="disabled",
        )

    with pytest.raises(WorkflowStrategyFallbackError, match="never select 'compiled_graph'"):
        attempt_strategy_fallback(
            requested_policy="auto",
            from_strategy="dynamic_tasks",
            to_strategy="compiled_graph",
            effect_state=LifecycleEffectState.NOT_STARTED,
            eligibility=plan.plan.eligibility,
        )


def test_observability_preserves_selection_and_rejections_when_reporting_disabled(settings) -> None:
    settings.DJANGO_RAY = {
        **getattr(settings, "DJANGO_RAY", {}),
        "WORKFLOW_PLAN_CODE_REVISION": "rev-1",
    }
    wf = chain(
        step(_add_one),
        map_step(_multiply, factor=2).with_limits(max_items=4, max_concurrency=2),
    )
    obs = wf.inspect_strategy(
        1,
        use_ray=False,
        requested_policy="local",
        reporting_policy="disabled",
    )
    payload = obs.as_dict()
    assert payload["plan_fingerprint"].startswith("sha256:")
    assert payload["plan_selection"]["selected_strategy"] == "local"
    assert payload["plan_selection"]["reporting_policy"] == "disabled"
    rejection_codes = {item["code"] for item in payload["plan_selection"]["rejections"]}
    assert "DYNAMIC_TOPOLOGY" in rejection_codes


def test_explicit_region_spec_validates_whole_plan_and_rejects_mixed_regions(settings) -> None:
    settings.DJANGO_RAY = {
        **getattr(settings, "DJANGO_RAY", {}),
        "WORKFLOW_PLAN_CODE_REVISION": "rev-1",
    }
    wf = chain(step(_add_one), step(_multiply, factor=2))
    whole_region = WorkflowRegionSpec(
        region_id="whole-static",
        node_ids=("0.0", "0.1"),
        requested_strategy="local",
    )
    assert wf.with_execution_strategy("local", regions=(whole_region,)).run(3, use_ray=False) == 8

    mixed_regions = (
        WorkflowRegionSpec(region_id="r0", node_ids=("0.0",), requested_strategy="local"),
        WorkflowRegionSpec(region_id="r1", node_ids=("0.1",), requested_strategy="dynamic_tasks"),
    )
    with pytest.raises(WorkflowStrategySelectionError, match="MIXED_REGIONS_UNSUPPORTED"):
        wf.with_execution_strategy("local", regions=mixed_regions).run(3, use_ray=False)


def test_fixed_actor_builder_supports_chain_group_and_worker_pool() -> None:
    wf = chain(
        group(step(_add_one), step(_multiply, factor=4)),
        step(_sum_pair),
    )
    plan = materialize_workflow_plan(wf, invocation_args=(3,), build_context=_clean_build_context())
    fixed_plan, rejections = build_fixed_actor_graph_plan(
        wf,
        plan,
        pool_specs={"0.0.g0": StaticActorPoolSpec(replicas=2)},
    )
    assert rejections == ()
    assert fixed_plan is not None
    assert fixed_plan.topology_kind == "worker_pool"
    assert len(fixed_plan.stages) == 3
    assert fixed_plan.stages[0].replicas == 2
    assert fixed_plan.stages[1].replicas == 1
    assert fixed_plan.stages[2].dependencies == ("actor_stage:0", "actor_stage:1")


def test_fixed_actor_builder_rejects_dynamic_map_django_and_async() -> None:
    dynamic_wf = map_step(_add_one).with_limits(max_items=4, max_concurrency=2)
    dynamic_plan = materialize_workflow_plan(
        dynamic_wf,
        invocation_args=([1, 2],),
        build_context=_clean_build_context(),
    )
    fixed, rejections = build_fixed_actor_graph_plan(dynamic_wf, dynamic_plan)
    assert fixed is None
    assert any(r.code == "DYNAMIC_TOPOLOGY" for r in rejections)

    django_wf = step(_add_one, django=True)
    django_plan = materialize_workflow_plan(
        django_wf,
        invocation_args=(1,),
        build_context=_clean_build_context(),
    )
    fixed_dj, rejections_dj = build_fixed_actor_graph_plan(django_wf, django_plan)
    assert fixed_dj is None
    assert any(r.code == "DJANGO_ORM_UNSUPPORTED" for r in rejections_dj)

    async_wf = step(_async_leaf)
    async_plan = materialize_workflow_plan(
        async_wf,
        invocation_args=(1,),
        build_context=_clean_build_context(),
    )
    fixed_async, rejections_async = build_fixed_actor_graph_plan(async_wf, async_plan)
    assert fixed_async is None
    assert any(r.code == "UNSUPPORTED_CALLABLE_KIND" for r in rejections_async)


def test_compiled_graph_strategy_is_default_off_and_never_selected_by_auto(settings) -> None:
    settings.DJANGO_RAY = {
        **getattr(settings, "DJANGO_RAY", {}),
        "COMPILED_GRAPH_EXPERIMENTAL_ENABLED": False,
        "WORKFLOW_PLAN_CODE_REVISION": "rev-1",
    }
    wf = chain(step(_add_one), step(_multiply, factor=2))
    with pytest.raises(WorkflowStrategySelectionError) as exc_info:
        wf.with_execution_strategy(
            "compiled_graph",
            compiled_graph_capability_decision=_verified_capability_decision(),
        ).run(5)
    assert any(r.code == "EXPERIMENTAL_GATE_DISABLED" for r in exc_info.value.rejections)

    # Even with the experimental gate enabled and a verified capability decision,
    # auto mode must NEVER select compiled_graph.
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "COMPILED_GRAPH_EXPERIMENTAL_ENABLED": True,
    }
    plan = materialize_workflow_plan(wf, invocation_args=(5,), build_context=_clean_build_context())
    strategy, selection = resolve_execution_strategy(
        wf,
        plan,
        use_ray=False,
        requested_policy=WorkflowRequestedStrategyPolicy.AUTO.value,
        compiled_graph_capability_decision=_verified_capability_decision(),
    )
    assert selection.selected_strategy == WorkflowExecutionStrategyName.LOCAL.value
    assert strategy.strategy_name is WorkflowExecutionStrategyName.LOCAL


def test_compiled_graph_strategy_fails_closed_when_capability_unverified(settings) -> None:
    settings.DJANGO_RAY = {
        **getattr(settings, "DJANGO_RAY", {}),
        "COMPILED_GRAPH_EXPERIMENTAL_ENABLED": True,
        "WORKFLOW_PLAN_CODE_REVISION": "rev-1",
    }
    wf = step(_add_one)
    plan = materialize_workflow_plan(wf, invocation_args=(5,), build_context=_clean_build_context())
    cg = CompiledGraphExecutionStrategy(signature=wf)
    with pytest.raises(WorkflowStrategySelectionError) as exc_info:
        cg.validate(plan, requested_policy="compiled_graph")
    assert exc_info.value.rejections
    assert exc_info.value.rejections[0].strategy == "compiled_graph"


def test_compiled_graph_strategy_executes_hermetic_fixed_actor_lifecycle_and_cleans_up(
    settings,
) -> None:
    settings.DJANGO_RAY = {
        **getattr(settings, "DJANGO_RAY", {}),
        "COMPILED_GRAPH_EXPERIMENTAL_ENABLED": True,
        "WORKFLOW_PLAN_CODE_REVISION": "rev-1",
    }
    wf = chain(
        group(step(_add_one), step(_multiply, factor=5)),
        step(_sum_pair),
    )
    plan = materialize_workflow_plan(wf, invocation_args=(4,), build_context=_clean_build_context())
    run_id = WorkflowRunIdentity(
        task_execution_pk=77,
        attempt_number=1,
        execution_generation=1,
        run_id="run-77",
    )
    cg = CompiledGraphExecutionStrategy(
        signature=wf,
        pool_specs={"0.0.g0": StaticActorPoolSpec(replicas=2)},
        capability_decision=_verified_capability_decision(),
    )
    selection = cg.validate(plan, requested_policy="compiled_graph", reporting_policy="disabled")
    prepared = cg.prepare(plan, selection=selection, run_identity=run_id)
    assert prepared.lifecycle_state is not None
    assert prepared.lifecycle_state.session_state is CompiledGraphSessionState.READY

    admitted = cg.admit(prepared)
    assert admitted.lifecycle_state is not None
    assert admitted.lifecycle_state.invocation_state is CompiledGraphInvocationState.ADMITTED

    handle = cg.execute(admitted, wf, (4,), {})
    assert isinstance(handle, CompiledGraphStrategyHandle)
    assert handle.lifecycle_state.effect_state is LifecycleEffectState.MAY_HAVE_STARTED

    result = cg.consume_result(handle, expected_run_identity=run_id)
    assert result == 25  # (4 + 1) + (4 * 5)
    assert handle.lifecycle_state.primary_outcome is LifecycleOutcome.SUCCEEDED

    cg.drain(handle, expected_run_identity=run_id)
    cg.cleanup(handle, expected_run_identity=run_id)
    assert handle.lifecycle_state.session_state is CompiledGraphSessionState.TORN_DOWN

    # Verify all per-run actor replicas cleared their state and caches on teardown
    adapter = handle._adapter
    for replicas in adapter.replicas_by_stage.values():
        for replica in replicas:
            assert replica.cleaned_up is True
            assert replica.cached_inputs == []
            assert replica.cached_outputs == []
            assert replica.invocation_context == {}

    # Also verify ergonomic with_execution_strategy("compiled_graph").run(...)
    with durable_task_execution(
        78,
        task_id="task-78",
        attempt_number=1,
        execution_generation=1,
        ray_job_driver=True,
        compiled_graph_submission_transport="ray-job",
    ):
        # Use clean build context via monkeypatch to avoid unresolved git/image in unit test
        import django_ray.workflow.plans as plans_module

        orig_default_ctx = plans_module._default_build_context
        plans_module._default_build_context = lambda _cfg, _tc: _clean_build_context()
        try:
            out = wf.with_execution_strategy(
                "compiled_graph",
                pool_specs={"0.0.g0": StaticActorPoolSpec(replicas=2)},
                compiled_graph_capability_decision=_verified_capability_decision(),
            ).run(6)
        finally:
            plans_module._default_build_context = orig_default_ctx
    assert out == 37  # (6 + 1) + (6 * 5)


def test_compiled_graph_experimental_enabled_rejects_non_bool() -> None:
    with pytest.raises(ImproperlyConfigured, match="COMPILED_GRAPH_EXPERIMENTAL_ENABLED"):
        compiled_graph_experimental_enabled({"COMPILED_GRAPH_EXPERIMENTAL_ENABLED": "yes"})


def test_dynamic_tasks_strategy_and_region_pool_validation_edges(settings) -> None:

    from django_ray.runtime.compiled_graph_lifecycle import LifecycleAction, LifecycleActionKind
    from django_ray.workflow.plans import WorkflowPlanValidationError
    from django_ray.workflow.strategy import (
        DynamicTasksStrategyHandle,
        DynamicTasksWorkflowExecutionStrategy,
        InProcessFixedActorAdapter,
    )

    settings.DJANGO_RAY = {
        **getattr(settings, "DJANGO_RAY", {}),
        "WORKFLOW_PLAN_CODE_REVISION": "rev-1",
    }

    # WorkflowRegionSpec and StaticActorPoolSpec validation
    with pytest.raises(WorkflowPlanValidationError, match="region_id must be non-empty"):
        WorkflowRegionSpec(region_id=" ", node_ids=("0",), requested_strategy="local")
    with pytest.raises(WorkflowPlanValidationError, match="node_ids must be a non-empty"):
        WorkflowRegionSpec(region_id="r1", node_ids=(), requested_strategy="local")
    with pytest.raises(WorkflowPlanValidationError, match="requested_strategy"):
        WorkflowRegionSpec(region_id="r1", node_ids=("0",), requested_strategy="invalid")
    with pytest.raises(WorkflowPlanValidationError, match="replicas must be an integer >= 1"):
        StaticActorPoolSpec(replicas=0)
    with pytest.raises(WorkflowPlanValidationError, match="must not exceed 64"):
        StaticActorPoolSpec(replicas=65)

    wf = chain(step(_add_one), step(_multiply, factor=2))
    plan = materialize_workflow_plan(wf, invocation_args=(3,), build_context=_clean_build_context())
    run_id = WorkflowRunIdentity(
        task_execution_pk=99,
        attempt_number=1,
        execution_generation=1,
        run_id="run-99",
    )

    # Region unknown node and overlapping node validation
    with pytest.raises(WorkflowPlanValidationError, match="unknown plan node"):
        LocalWorkflowExecutionStrategy().validate(
            plan,
            requested_policy="local",
            regions=(
                WorkflowRegionSpec(region_id="r1", node_ids=("999",), requested_strategy="local"),
            ),
        )
    with pytest.raises(WorkflowPlanValidationError, match="overlaps already-assigned"):
        LocalWorkflowExecutionStrategy().validate(
            plan,
            requested_policy="local",
            regions=(
                WorkflowRegionSpec(region_id="r1", node_ids=("0.0",), requested_strategy="local"),
                WorkflowRegionSpec(
                    region_id="r2", node_ids=("0.0", "0.1"), requested_strategy="local"
                ),
            ),
        )

    # LocalWorkflowExecutionStrategy cancel and post-cancel consume_result failure
    local_strategy_inst = LocalWorkflowExecutionStrategy()
    local_sel = local_strategy_inst.validate(plan, requested_policy="local")
    local_prep = local_strategy_inst.prepare(plan, selection=local_sel, run_identity=run_id)
    local_adm = local_strategy_inst.admit(local_prep)
    local_handle = local_strategy_inst.execute(local_adm, wf, (3,), {})
    local_strategy_inst.cancel(local_handle, expected_run_identity=run_id)
    with pytest.raises(RuntimeError, match="no longer active"):
        local_strategy_inst.consume_result(local_handle, expected_run_identity=run_id)

    # DynamicTasksWorkflowExecutionStrategy full lifecycle with fake executor
    class _FakeDynamicExecutor:
        def __init__(self) -> None:
            self.bound = False
            self.finished_failed: list[bool] = []
            self.cancelled_refs: list[Any] = []

        def bind_plan(self, *_args: Any, **_kwargs: Any) -> None:
            self.bound = True

        def submit_step(
            self, step_sig: Any, args: tuple[Any, ...], kwargs: dict[str, Any], *_a: Any
        ) -> Any:
            del kwargs
            return args[0] + 1 if step_sig.callable_path.endswith("_add_one") else args[0] * 2

        def resolve(self, value: Any) -> Any:
            return value

        def track_cleanup(self, _value: Any) -> None:
            return None

        def cancel_and_drain(self, refs: list[Any], *, timeout_seconds: float = 5.0) -> None:
            del timeout_seconds
            self.cancelled_refs.extend(refs)

        def finish_progress(self, *, failed: bool = False) -> None:
            self.finished_failed.append(failed)

    dyn = DynamicTasksWorkflowExecutionStrategy()
    dyn_sel = dyn.validate(plan, requested_policy="dynamic_tasks", reporting_policy="disabled")
    dyn_prep = dyn.prepare(plan, selection=dyn_sel, run_identity=run_id)
    dyn_adm = dyn.admit(dyn_prep)
    fake_exec = _FakeDynamicExecutor()
    dyn_handle = dyn.execute(dyn_adm, wf, (3,), {}, executor=fake_exec)
    assert isinstance(dyn_handle, DynamicTasksStrategyHandle)
    assert dyn_handle.strategy_name == "dynamic_tasks"
    assert dyn.consume_result(dyn_handle, expected_run_identity=run_id) == 8
    with pytest.raises(RuntimeError, match="already been consumed"):
        dyn.consume_result(dyn_handle, expected_run_identity=run_id)

    # Cancel and cleanup on a second dynamic handle
    dyn_handle2 = dyn.execute(dyn_adm, wf, (4,), {}, executor=fake_exec)
    dyn.cancel(dyn_handle2, expected_run_identity=run_id)
    dyn.cancel(dyn_handle2, expected_run_identity=run_id)  # idempotent
    assert fake_exec.cancelled_refs == [10]
    with pytest.raises(RuntimeError, match="no longer active"):
        dyn.consume_result(dyn_handle2, expected_run_identity=run_id)
    dyn.drain(dyn_handle2, expected_run_identity=run_id)
    dyn.cleanup(dyn_handle2, expected_run_identity=run_id)

    # FixedActorGraphPlan.as_dict(), InProcessFixedActorAdapter extra actions, and scatter/gather group
    group_wf = group(step(_add_one), step(_multiply, factor=3))
    group_plan = materialize_workflow_plan(
        group_wf, invocation_args=(2,), build_context=_clean_build_context()
    )
    fixed_group, group_rej = build_fixed_actor_graph_plan(group_wf, group_plan)
    assert group_rej == ()
    assert fixed_group is not None
    assert fixed_group.as_dict()["topology_kind"] == "scatter_gather"
    adapter = InProcessFixedActorAdapter(fixed_group)
    adapter.dispatch(
        LifecycleAction(
            kind=LifecycleActionKind.PREPARE,
            session_identity=run_id,
            token="t-prep",
            deadline=10.0,
        )
    )
    adapter.bind_invocation_inputs((2,), {})
    adapter.dispatch(
        LifecycleAction(
            kind=LifecycleActionKind.SUBMIT,
            session_identity=run_id,
            token="t-sub",
            deadline=10.0,
        )
    )
    assert adapter.pop_consumed_output() == [3, 6]
    adapter.dispatch(
        LifecycleAction(
            kind=LifecycleActionKind.CHECK_HEALTH,
            session_identity=run_id,
            token="t-health",
            deadline=10.0,
        )
    )
    adapter.dispatch(
        LifecycleAction(
            kind=LifecycleActionKind.RELEASE_CAPACITY,
            session_identity=run_id,
            token="t-rel",
            deadline=10.0,
        )
    )
    adapter.dispatch(
        LifecycleAction(
            kind=LifecycleActionKind.CANCEL,
            session_identity=run_id,
            token="t-cancel",
            deadline=10.0,
        )
    )

    # CompiledGraphExecutionStrategy cancel, observability with lifecycle_state, and resolve_execution_strategy branches
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "COMPILED_GRAPH_EXPERIMENTAL_ENABLED": True,
    }
    cg = CompiledGraphExecutionStrategy(
        signature=group_wf,
        capability_decision=_verified_capability_decision(),
    )
    cg_sel = cg.validate(group_plan, requested_policy="compiled_graph", reporting_policy="disabled")
    cg_prep = cg.prepare(group_plan, selection=cg_sel, run_identity=run_id)
    obs = cg.observability(
        group_plan,
        cg_sel,
        lifecycle_state=cg_prep.lifecycle_state,
    )
    assert "lifecycle_snapshot" in obs.as_dict()
    cg_adm = cg.admit(cg_prep)
    cg_handle = cg.execute(cg_adm, group_wf, (2,), {})
    assert cg_handle.strategy_name == "compiled_graph"
    assert cg_handle.invocation_id == cg_adm.invocation_identity.invocation_id
    cg.cancel(cg_handle, expected_run_identity=run_id)
    with pytest.raises(RuntimeError, match="no longer active"):
        cg.consume_result(cg_handle, expected_run_identity=run_id)
    cg.cleanup(cg_handle, expected_run_identity=run_id)

    with pytest.raises(
        WorkflowPlanValidationError, match="Unsupported workflow execution strategy"
    ):
        resolve_execution_strategy(wf, plan, use_ray=False, requested_policy="bad_policy")
    with pytest.raises(WorkflowPlanValidationError, match="cannot be combined with use_ray=False"):
        resolve_execution_strategy(wf, plan, use_ray=False, requested_policy="compiled_graph")
    with pytest.raises(WorkflowStrategySelectionError, match="static_actors"):
        resolve_execution_strategy(wf, plan, use_ray=True, requested_policy="static_actors")
    s_dyn, sel_dyn = resolve_execution_strategy(
        wf, plan, use_ray=True, requested_policy="dynamic_tasks", reporting_policy="disabled"
    )
    assert s_dyn.strategy_name is WorkflowExecutionStrategyName.DYNAMIC_TASKS
    assert sel_dyn.selected_strategy == "dynamic_tasks"


def test_strategy_builder_and_execution_error_branches(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    import django_ray.workflows as workflows_mod
    from django_ray.runtime.compiled_graph_lifecycle import LifecycleAction, LifecycleActionKind
    from django_ray.workflow.strategy import (
        DynamicTasksWorkflowExecutionStrategy,
        InProcessFixedActorAdapter,
        _default_run_identity,
        _IsolatedStageActorReplica,
    )

    settings.DJANGO_RAY = {
        **getattr(settings, "DJANGO_RAY", {}),
        "COMPILED_GRAPH_EXPERIMENTAL_ENABLED": True,
        "WORKFLOW_PLAN_CODE_REVISION": "rev-1",
    }

    # _default_run_identity inside durable_task_execution
    with durable_task_execution(55, task_id="t-55", attempt_number=2, execution_generation=3):
        rid = _default_run_identity()
        assert rid.task_execution_pk == 55
        assert rid.attempt_number == 2
        assert rid.execution_generation == 3

    # Nested group(group(...)) rejection and unimportable callable rejection in build_fixed_actor_graph_plan
    nested_wf = group(group(step(_add_one), step(_multiply)))
    nested_plan = materialize_workflow_plan(
        nested_wf, invocation_args=(1,), build_context=_clean_build_context()
    )
    fixed_nested, rej_nested = build_fixed_actor_graph_plan(nested_wf, nested_plan)
    assert fixed_nested is None
    assert any(r.code == "NESTED_GROUP_GATHER_UNSUPPORTED" for r in rej_nested)

    bad_step = step("nonexistent.module.missing_fn")
    bad_plan = materialize_workflow_plan(
        bad_step, invocation_args=(1,), build_context=_clean_build_context()
    )
    fixed_bad, rej_bad = build_fixed_actor_graph_plan(bad_step, bad_plan)
    assert fixed_bad is None
    assert any(r.code == "UNRESOLVED_CODE_IDENTITY" for r in rej_bad)

    # _IsolatedStageActorReplica invoke after clear_state() and InProcessFixedActorAdapter error branches
    wf = step(_add_one)
    plan = materialize_workflow_plan(wf, invocation_args=(1,), build_context=_clean_build_context())
    fixed_single, _ = build_fixed_actor_graph_plan(wf, plan)
    assert fixed_single is not None
    replica = _IsolatedStageActorReplica(fixed_single.stages[0], 0)
    replica.clear_state()
    with pytest.raises(RuntimeError, match="has been torn down"):
        replica.invoke((1,), {})

    adapter = InProcessFixedActorAdapter(fixed_single)
    run_id = _default_run_identity()
    adapter._has_buffered_output = True
    cap_ev = adapter.dispatch(
        LifecycleAction(
            kind=LifecycleActionKind.ADMIT,
            session_identity=run_id,
            token="t-adm",
            deadline=10.0,
        )
    )
    assert cap_ev.kind.value == "CAPACITY_REJECTED"
    with pytest.raises(ValueError, match="Unsupported lifecycle action"):
        adapter.dispatch(SimpleNamespace(kind="unsupported"))  # type: ignore[arg-type]

    # LocalWorkflowExecutionStrategy and DynamicTasksWorkflowExecutionStrategy failure handling
    # LocalWorkflowExecutionStrategy and DynamicTasksWorkflowExecutionStrategy failure handling
    boom_wf = step(_boom)

    boom_wf = step(_boom)
    boom_plan = materialize_workflow_plan(
        boom_wf, invocation_args=(1,), build_context=_clean_build_context()
    )
    loc = LocalWorkflowExecutionStrategy()
    loc_sel = loc.validate(boom_plan, requested_policy="local")
    loc_adm = loc.admit(loc.prepare(boom_plan, selection=loc_sel))
    with pytest.raises(ValueError, match="step exploded"):
        loc.execute(loc_adm, boom_wf, (1,), {})

    class _FailingRayExecutor:
        def __init__(self, *, fail_on_submit: bool = False) -> None:
            self.fail_on_submit = fail_on_submit
            self.failed_calls: list[bool] = []

        def bind_plan(self, *_a: Any, **_kw: Any) -> None:
            return None

        def submit_step(self, *_a: Any, **_kw: Any) -> Any:
            if self.fail_on_submit:
                raise RuntimeError("submit exploded")
            return "ref-1"

        def track_cleanup(self, _v: Any) -> None:
            return None

        def resolve(self, _v: Any) -> Any:
            raise RuntimeError("resolve exploded")

        def finish_progress(self, *, failed: bool = False) -> None:
            self.failed_calls.append(failed)

    dyn = DynamicTasksWorkflowExecutionStrategy()
    dyn_sel = dyn.validate(plan, requested_policy="dynamic_tasks", reporting_policy="disabled")
    dyn_adm = dyn.admit(dyn.prepare(plan, selection=dyn_sel))
    with pytest.raises(RuntimeError, match="submit exploded"):
        dyn.execute(dyn_adm, wf, (1,), {}, executor=_FailingRayExecutor(fail_on_submit=True))

    h_fail = dyn.execute(dyn_adm, wf, (1,), {}, executor=_FailingRayExecutor(fail_on_submit=False))
    with pytest.raises(RuntimeError, match="resolve exploded"):
        dyn.consume_result(h_fail)

    # CompiledGraphExecutionStrategy wrong policy, ineligible prepare, and duplicate consume_result
    cg = CompiledGraphExecutionStrategy(
        signature=wf,
        capability_decision=_verified_capability_decision(),
    )
    with pytest.raises(WorkflowStrategySelectionError, match="explicit requested_policy"):
        cg.validate(plan, requested_policy="auto")
    with pytest.raises(WorkflowStrategySelectionError, match="refused an ineligible plan"):
        cg.prepare(
            plan,
            selection=loc_sel,
            config={"COMPILED_GRAPH_EXPERIMENTAL_ENABLED": False},
        )
    cg_sel = cg.validate(plan, requested_policy="compiled_graph", reporting_policy="disabled")
    cg_adm = cg.admit(cg.prepare(plan, selection=cg_sel))
    cg_h = cg.execute(cg_adm, wf, (4,), {})
    assert cg.consume_result(cg_h) == 5
    with pytest.raises(RuntimeError, match="already been consumed"):
        cg.consume_result(cg_h)
    cg.drain(cg_h)
    cg.drain(cg_h)  # idempotent
    cg.cleanup(cg_h)
    cg.cleanup(cg_h)  # idempotent

    # resolve_execution_strategy in AUTO mode when _get_executor returns a non-_LocalExecutor
    monkeypatch.setattr(workflows_mod, "_get_executor", lambda _use_ray: _FailingRayExecutor())
    auto_s, auto_sel = resolve_execution_strategy(
        wf,
        plan,
        use_ray=None,
        requested_policy="auto",
        reporting_policy="disabled",
    )
    assert auto_s.strategy_name is WorkflowExecutionStrategyName.DYNAMIC_TASKS
    assert auto_sel.selected_strategy == "dynamic_tasks"
