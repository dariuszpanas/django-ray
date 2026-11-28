"""Unit tests for workflow fault tolerance, async steps, OOM safety, and strategy fixes."""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest

from django_ray.runtime.compiled_graph import (
    COMPILED_GRAPH_POLICY_VERSION,
    CompiledGraphCapabilityDecision,
    CompiledGraphReason,
    CompiledGraphRuntimeIdentity,
    CompiledGraphSubmissionTransport,
    CompiledGraphTopology,
    CompiledGraphTransport,
)
from django_ray.runtime.context import (
    WorkflowRunIdentity,
    WorkflowStepContext,
    _current_workflow_step,
    durable_task_execution,
    report_workflow_progress,
)
from django_ray.runtime.remote import _execute_workflow_step
from django_ray.workflow.plans import WorkflowPlanBuildContext, materialize_workflow_plan
from django_ray.workflow.strategy import (
    CompiledGraphExecutionStrategy,
    DynamicTasksWorkflowExecutionStrategy,
    LocalWorkflowExecutionStrategy,
    WorkflowStrategySelectionError,
    build_fixed_actor_graph_plan,
    resolve_execution_strategy,
)
from django_ray.workflows import (
    _get_executor,
    _LocalExecutor,
    _RayExecutor,
    chain,
    group,
    map_step,
    step,
)


async def _async_double(value: int) -> int:
    return value * 2


def _subtract(left: int, right: int) -> int:
    return left - right


def _identity(value: Any) -> Any:
    return value


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


def _verified_decision() -> CompiledGraphCapabilityDecision:
    return CompiledGraphCapabilityDecision(
        eligible=True,
        verified=True,
        reason=CompiledGraphReason.ELIGIBLE,
        message="Hermetic unit-test decision",
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


class TestWorkflowAutoInitAndAsyncSteps:
    """F-023 & F-024: Ray Job driver auto-init, no UnboundLocalError, async steps."""

    def test_get_executor_auto_inits_ray_for_use_ray_none_in_ray_job_driver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inited: list[dict[str, Any]] = []
        fake_ray = ModuleType("ray")
        state = {"initialized": False}

        def fake_is_initialized() -> bool:
            return state["initialized"]

        def fake_init(**kwargs: Any) -> None:
            inited.append(kwargs)
            state["initialized"] = True

        fake_ray.is_initialized = fake_is_initialized  # type: ignore[attr-defined]
        fake_ray.init = fake_init  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "ray", fake_ray)

        class _SentinelRayExecutor:
            pass

        monkeypatch.setattr("django_ray.workflows._RayExecutor", _SentinelRayExecutor)

        with durable_task_execution(
            99,
            task_id="00000000-0000-4000-8000-000000000099",
            attempt_number=1,
            execution_generation=1,
            ray_job_driver=True,
        ):
            executor = _get_executor(None)

        assert isinstance(executor, _SentinelRayExecutor)
        assert inited == [{"address": "auto", "ignore_reinit_error": True}]

    def test_get_executor_raises_runtime_error_not_unbound_local_when_ray_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "ray", None)
        with durable_task_execution(
            100,
            task_id="00000000-0000-4000-8000-000000000100",
            attempt_number=1,
            execution_generation=1,
            ray_job_driver=True,
        ):
            with pytest.raises(RuntimeError, match="use_ray=True requires Ray"):
                _get_executor(True)

    def test_async_workflow_step_executes_locally_and_in_remote_step_helper(self) -> None:
        wf = chain(step(_async_double), step(_async_double))
        assert wf.run(5, use_ray=False) == 20

        remote_out = _execute_workflow_step(
            f"{__name__}._async_double",
            False,
            (),
            {},
            {},
            None,
            None,
            "0",
            6,
        )
        assert remote_out == 12


class TestWorkflowFaultToleranceAndPerf:
    """F-016, F-025, F-038, & F-093."""

    def test_group_partial_failure_cancels_active_siblings_without_ray_get(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cancelled_payload_safe: list[Any] = []
        finished_failed: list[bool] = []

        class _RecordingExecutor(_LocalExecutor):
            def submit_step(self, signature, input_args, input_kwargs, node_id, dependencies):
                del signature, input_args, input_kwargs, dependencies
                return f"ref:{node_id}"

            def collect(self, values: list[Any]) -> Any:
                return ("collected", tuple(values))

            def resolve(self, value: Any) -> Any:
                del value
                raise RuntimeError("branch 0 failed")

            def cancel_and_drain_fold_payloads(
                self, values: Any, *, timeout_seconds: float
            ) -> None:
                del timeout_seconds
                cancelled_payload_safe.extend(values)

            def finish_progress(self, *, failed: bool = False) -> None:
                finished_failed.append(failed)

        monkeypatch.setattr("django_ray.workflows._get_executor", lambda _u: _RecordingExecutor())
        wf = group(step(_identity), step(_identity))

        with pytest.raises(RuntimeError, match="branch 0 failed"):
            wf.run(1, use_ray=True)

        assert "ref:0.g0" in cancelled_payload_safe
        assert "ref:0.g1" in cancelled_payload_safe
        assert finished_failed == [True]

    def test_result_buffer_error_cleanup_never_calls_cancel_and_drain_ray_get(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload_safe_calls: list[list[Any]] = []
        unsafe_calls: list[list[Any]] = []

        class _BufferOverflowExecutor(_LocalExecutor):
            def start_result_buffer(self, **_kwargs: Any) -> Any:
                return object()

            def submit_step(self, signature, input_args, input_kwargs, node_id, dependencies):
                del signature, input_kwargs, dependencies
                return f"oversized-ref:{node_id}:{input_args[0]}"

            def wait_result_buffer_leaf(self, values: Any) -> int:
                del values
                return 0

            def append_result_buffer(self, buffer: Any, *, index: int, value: Any) -> None:
                del buffer, index, value
                raise RuntimeError("ResultBufferOverflowError")

            def cancel_and_drain_fold_payloads(
                self, values: Any, *, timeout_seconds: float
            ) -> None:
                del timeout_seconds
                payload_safe_calls.append(list(values))

            def cancel_and_drain(self, values: Any, *, timeout_seconds: float) -> None:
                del timeout_seconds
                unsafe_calls.append(list(values))

            def discard_result_buffer(self, buffer: Any, *, timeout_seconds: float) -> None:
                del buffer, timeout_seconds

        monkeypatch.setattr(
            "django_ray.workflows._get_executor", lambda _u: _BufferOverflowExecutor()
        )
        wf = (
            map_step(_identity)
            .with_limits(max_items=4, max_concurrency=2)
            .with_result_buffer(
                max_serialized_bytes=1024,
                actor_options={"num_cpus": 1, "memory": 64 * 1024 * 1024},
            )
        )

        with pytest.raises(RuntimeError, match="ResultBufferOverflowError"):
            wf.run([1, 2, 3], use_ray=True)

        assert unsafe_calls == []
        assert len(payload_safe_calls) >= 1
        assert any("oversized-ref:" in str(ref) for call in payload_safe_calls for ref in call)

    def test_resolve_and_wait_loops_do_not_call_dead_weight_flush_progress(self) -> None:
        executor = object.__new__(_RayExecutor)
        snapshot_calls = 0
        poll_calls = 0

        class _FakeActor:
            class _Snapshot:
                @staticmethod
                def remote() -> Any:
                    nonlocal snapshot_calls
                    snapshot_calls += 1
                    return "snap-ref"

            snapshot = _Snapshot()

        wait_results = iter([([], ["val"]), (["val"], [])])

        class _StubRay:
            @staticmethod
            def wait(_refs: Any, **_kwargs: Any) -> tuple[list[Any], list[Any]]:
                return next(wait_results)

            @staticmethod
            def get(ref: Any) -> Any:
                return f"resolved:{ref}"

        executor.ray = _StubRay()
        executor.progress_actor = _FakeActor()
        executor._poll_leaf_outcomes = lambda: (_ for _ in ()).throw(AssertionError)  # type: ignore[method-assign]

        def count_poll() -> None:
            nonlocal poll_calls
            poll_calls += 1

        executor._poll_leaf_outcomes = count_poll  # type: ignore[method-assign]

        assert executor.resolve("val") == "resolved:val"
        assert poll_calls == 2
        assert snapshot_calls == 0

    def test_report_workflow_progress_rejects_nan_and_bool(self) -> None:
        class _DummyProducer:
            def offer(self, *_a: Any, **_kw: Any) -> bool:
                return True

        ctx = WorkflowStepContext(
            progress_actor=object(),
            node_id="0",
            run_identity={"run_id": "r1"},
            producer=_DummyProducer(),
        )
        token = _current_workflow_step.set(ctx)
        try:
            with pytest.raises(ValueError, match="finite"):
                report_workflow_progress(float("nan"), 10)
            with pytest.raises(ValueError, match="finite"):
                report_workflow_progress(1, float("nan"))
            with pytest.raises(ValueError, match="finite"):
                report_workflow_progress(True, 10)
        finally:
            _current_workflow_step.reset(token)


class TestWorkflowStrategySeamFixes:
    """F-028, F-069, & F-093: Strategy fixes, arg order, deadlines, single bind_plan."""

    def test_compiled_graph_preserves_positional_arg_order_and_monotonic_deadlines(
        self, settings: Any
    ) -> None:
        settings.DJANGO_RAY = {
            **getattr(settings, "DJANGO_RAY", {}),
            "COMPILED_GRAPH_EXPERIMENTAL_ENABLED": True,
            "WORKFLOW_PLAN_CODE_REVISION": "rev-1",
        }
        # _subtract(input_arg=10, bound_arg=3) must equal 10 - 3 = 7 (not 3 - 10 = -7!)
        wf = step(_subtract, 3)
        plan = materialize_workflow_plan(
            wf, invocation_args=(10,), build_context=_clean_build_context()
        )
        cg = CompiledGraphExecutionStrategy(
            signature=wf,
            capability_decision=_verified_decision(),
            clock=lambda: 100.0,
        )
        sel = cg.validate(plan, requested_policy="compiled_graph", reporting_policy="disabled")
        prep = cg.prepare(plan, selection=sel)
        assert prep.lifecycle_state is not None
        deadlines = prep.lifecycle_state.deadlines
        assert (
            deadlines.admission
            < deadlines.submission
            < deadlines.result
            < deadlines.drain
            < deadlines.teardown
            < deadlines.outer
        )

        adm = cg.admit(prep)
        handle = cg.execute(adm, wf, (10,), {})
        assert cg.consume_result(handle) == 7
        cg.drain(handle)
        cg.cleanup(handle)

    def test_public_resolve_execution_strategy_rejects_in_process_compiled_graph_and_static_actors(
        self, settings: Any
    ) -> None:
        settings.DJANGO_RAY = {
            **getattr(settings, "DJANGO_RAY", {}),
            "COMPILED_GRAPH_EXPERIMENTAL_ENABLED": True,
            "WORKFLOW_PLAN_CODE_REVISION": "rev-1",
        }
        wf = step(_subtract, 2)
        plan = materialize_workflow_plan(
            wf, invocation_args=(5,), build_context=_clean_build_context()
        )

        with pytest.raises(WorkflowStrategySelectionError) as cg_exc:
            resolve_execution_strategy(
                wf,
                plan,
                use_ray=True,
                requested_policy="compiled_graph",
                compiled_graph_capability_decision=_verified_decision(),
            )
        assert any(r.code == "IN_PROCESS_ADAPTER_DISALLOWED" for r in cg_exc.value.rejections)

        with pytest.raises(WorkflowStrategySelectionError, match="static_actors"):
            resolve_execution_strategy(wf, plan, use_ray=True, requested_policy="static_actors")

    def test_local_strategy_binds_plan_once_and_dynamic_adopts_db_run_identity(
        self, settings: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.DJANGO_RAY = {
            **getattr(settings, "DJANGO_RAY", {}),
            "WORKFLOW_PLAN_CODE_REVISION": "rev-1",
        }
        wf = step(_identity)
        plan = materialize_workflow_plan(
            wf, invocation_args=(1,), build_context=_clean_build_context()
        )

        bind_calls = 0
        original_bind = _LocalExecutor.bind_plan

        def counting_bind(self: Any, *a: Any, **kw: Any) -> None:
            nonlocal bind_calls
            bind_calls += 1
            return original_bind(self, *a, **kw)

        monkeypatch.setattr(_LocalExecutor, "bind_plan", counting_bind)
        loc = LocalWorkflowExecutionStrategy()
        sel = loc.validate(plan, requested_policy="local")
        handle = loc.execute(loc.admit(loc.prepare(plan, selection=sel)), wf, (1,), {})
        assert loc.consume_result(handle) == 1
        assert bind_calls == 1

        # DynamicTasksWorkflowExecutionStrategy adopts executor's DB-allocated UUIDv8 run identity
        db_identity = WorkflowRunIdentity(
            task_execution_pk=12,
            attempt_number=1,
            execution_generation=1,
            run_id="01950000-0000-8000-8000-000000000012",
        )

        class _AllocatingExecutor(_LocalExecutor):
            workflow_run_identity = None

            def bind_plan(self, *_a: Any, **_kw: Any) -> None:
                self.workflow_run_identity = db_identity

        dyn = DynamicTasksWorkflowExecutionStrategy()
        dyn_sel = dyn.validate(plan, requested_policy="dynamic_tasks", reporting_policy="disabled")
        dyn_handle = dyn.execute(
            dyn.admit(dyn.prepare(plan, selection=dyn_sel)),
            wf,
            (9,),
            {},
            executor=_AllocatingExecutor(),
        )
        assert dyn_handle.run_identity == db_identity
        assert dyn.consume_result(dyn_handle, expected_run_identity=db_identity) == 9
        # Fixed build_fixed_actor_graph_plan sanity check
        fixed, rejs = build_fixed_actor_graph_plan(wf, plan)
        assert fixed is not None and rejs == ()
