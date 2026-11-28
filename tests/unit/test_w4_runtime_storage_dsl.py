"""Wave 4 verification tests for runtime, storage, purge TOCTOU, DSL 2.0, and lifecycle fixes."""

from __future__ import annotations

import errno
import functools
import hashlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

import django_ray.input_storage as input_storage
import django_ray.payload_registry as payload_registry
import django_ray.result_storage as result_storage
import django_ray.runtime.import_utils as import_utils
import django_ray.workflows as workflows_module
from django_ray import ray_task
from django_ray.execution_codec import ExecutionIdentity, ExecutionRequest, encode_execution_request
from django_ray.input_storage import (
    load_task_input,
    prepare_task_input,
    register_task_input,
)
from django_ray.management.commands.django_ray_purge_inputs import Command as PurgeInputsCommand
from django_ray.models import (
    InputPayloadKind,
    InputPayloadState,
    RayTaskExecution,
    TaskInputPayload,
    TaskState,
    TaskWorkerLease,
    WorkflowProgressRunStorage,
)
from django_ray.ray_job_protocol import (
    STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX,
    coordination_sha256,
)
from django_ray.ray_job_request_storage import (
    RayJobRequestStorageError,
    RayJobRequestStorageRejection,
    prepare_ray_job_request,
    register_and_attach_ray_job_request,
    release_ray_job_request_reservation,
)
from django_ray.result_storage import (
    FilesystemResultStorage,
    ResultStorageError,
    validate_storage_configuration,
)
from django_ray.runner.base import SubmissionHandle
from django_ray.runner.ray_core import RayCoreRunner
from django_ray.runtime.compiled_graph_lifecycle import (
    CompiledGraphInvocationState,
    CompiledGraphSessionState,
    LifecycleActionKind,
    LifecycleDeadlines,
    LifecycleEffectState,
    LifecycleEvent,
    LifecycleEventKind,
    LifecycleOutcome,
    LifecycleTransition,
    WorkflowInvocationIdentity,
    initial_session,
    reduce_lifecycle,
)
from django_ray.runtime.context import WorkflowRunIdentity, durable_task_execution
from django_ray.runtime.result_buffer import WorkflowMapResultBuffer
from django_ray.runtime.result_fold import (
    RESULT_FOLD_CODEC,
    RESULT_FOLD_CODEC_VERSION,
    RESULT_FOLD_PROTOCOL,
    RESULT_FOLD_PROTOCOL_VERSION,
    ResultFoldOverflowError,
    WorkflowMapResultFold,
    normalize_result_fold_actor_options,
)
from django_ray.workflow.checkpoints import (
    CheckpointableNodeSpec,
    CheckpointFencingError,
    DurableStepCheckpointEnvelope,
    NodeResumabilityClass,
    SideEffectDisposition,
    WorkflowCheckpointResumePlanner,
)
from django_ray.workflow.plans import (
    WorkflowPlanBuildContext,
    _callable_code_identity,
    materialize_workflow_plan,
)
from django_ray.workflow.progress.storage import (
    PreparedWorkflowProgressTopologyPage,
    WorkflowProgressTopologyCollection,
    _upsert_topology_pages_batched,
)
from django_ray.workflow.strategy import (
    DynamicTasksWorkflowExecutionStrategy,
    InProcessFixedActorAdapter,
    LocalWorkflowExecutionStrategy,
    build_fixed_actor_graph_plan,
)
from django_ray.workflows import (
    Chain,
    Group,
    WorkflowDefinitionError,
    WorkflowExpr,
    branch,
    explain_workflow,
    map_step,
    parallel,
    pipe,
    reset_cached_workflow_remotes,
    step,
)

# ---------------------------------------------------------------------------
# Module-level fixture callables
# ---------------------------------------------------------------------------


def _add_one(value: int) -> int:
    return value + 1


def _double(value: int) -> int:
    return value * 2


def _triple(value: int) -> int:
    return value * 3


def _is_even(value: int) -> bool:
    return value % 2 == 0


def _fail_value_error(value: int) -> int:
    raise ValueError(f"bad:{value}")


def _recover_with_error(value: int, error: Exception) -> str:
    return f"recovered:{value}:{type(error).__name__}:{error}"


def _sum_reducer(acc: int, item: int) -> int:
    return acc + item


def _dict_mutate_and_fail(acc: dict[str, Any], item: int) -> dict[str, Any]:
    acc[f"k{item}"] = item
    if item == 1:
        acc["k0"] = 999
        raise RuntimeError("dict-boom")
    return acc


def _nested_dict_mutate_and_fail(acc: dict[str, list[int]], item: int) -> dict[str, list[int]]:
    acc["items"].append(item)
    if item == 1:
        raise RuntimeError("nested-boom")
    return acc


def _list_overwrite_and_fail(acc: list[int], item: int) -> list[int]:
    if item == 1:
        acc[0] = -1
        acc.append(item)
        raise RuntimeError("list-overwrite-boom")
    acc.append(item)
    return acc


def _dict_expand_value(acc: dict[str, str], item: int) -> dict[str, str]:
    acc[f"k{item}"] = "X" * 2048
    return acc


def _dict_overwrite_expand(acc: dict[str, str], item: int) -> dict[str, str]:
    acc["slot"] = "Y" * (2048 if item == 1 else 4)
    return acc


@ray_task(
    num_cpus=2,
    num_gpus=1,
    memory=512 * 1024 * 1024,
    runtime_env_profile="ml-gpu",
)
def _contract_gpu_task(value: int) -> int:
    return value * 2


class RayTaskError(Exception):
    """Minimal stand-in matching Ray's RayTaskError wrapper contract."""

    def __init__(self, cause: Exception) -> None:
        super().__init__(f"RayTaskError({cause})")
        self.cause = cause


# ---------------------------------------------------------------------------
# R2-002: Driver allowlist propagation into remote steps & WorkflowMapResultFold
# ---------------------------------------------------------------------------


def test_result_fold_resolves_reducer_via_explicit_allowed_callable_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulate bootstrap_django=False worker where Django settings are unconfigured.
    monkeypatch.setattr(import_utils, "_configured_allowed_callable_modules", lambda: None)
    monkeypatch.setattr(import_utils, "_default_workflow_allowed_modules", lambda: ())

    with pytest.raises(ImportError, match="not permitted"):
        WorkflowMapResultFold(
            4,
            2,
            4096,
            f"{__name__}._sum_reducer",
            False,
            (),
            {},
            0,
        )

    fold = WorkflowMapResultFold(
        4,
        2,
        4096,
        f"{__name__}._sum_reducer",
        False,
        (),
        {},
        0,
        allowed_callable_modules=(__name__,),
    )
    fold.append(0, 10)
    fold.append(1, 20)
    result, _ack = fold.finalize(2)
    assert result == 30


def test_workflow_driver_allowlist_propagates_to_remote_step_and_fold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        import_utils,
        "get_driver_allowed_callable_modules",
        lambda: ("custom.driver.mod", __name__),
        raising=False,
    )
    assert workflows_module._driver_allowed_callable_modules() == ("custom.driver.mod", __name__)

    captured_fold_kwargs: dict[str, Any] = {}

    class _FakeActorHandle:
        class _Ready:
            @staticmethod
            def remote() -> dict[str, Any]:
                return {
                    "protocol": RESULT_FOLD_PROTOCOL,
                    "protocol_version": RESULT_FOLD_PROTOCOL_VERSION,
                    "codec": RESULT_FOLD_CODEC,
                    "codec_version": RESULT_FOLD_CODEC_VERSION,
                    "state": "ready",
                    "folded_items": 0,
                    "out_of_order_items": 0,
                    "retained_bytes": 16,
                }

        ready = _Ready()

    class _FakeFoldActorClass:
        @staticmethod
        def options(**_opts: Any) -> Any:
            class _Remote:
                @staticmethod
                def remote(*_args: Any, **kwargs: Any) -> _FakeActorHandle:
                    captured_fold_kwargs.update(kwargs)
                    return _FakeActorHandle()

            return _Remote()

    executor = workflows_module._RayExecutor.__new__(workflows_module._RayExecutor)
    executor.ray = SimpleNamespace()
    executor.task_context = None
    executor.materialized_plan = None
    executor.resolve = lambda value: value  # type: ignore[method-assign]
    monkeypatch.setattr(
        workflows_module, "_get_cached_result_fold_actor", lambda: _FakeFoldActorClass
    )

    executor.start_result_fold(
        max_items=4,
        max_concurrency=2,
        max_serialized_bytes=4096,
        actor_options=normalize_result_fold_actor_options(
            {"num_cpus": 0.25, "memory": 8192},
            max_serialized_bytes=4096,
        ),
        reducer=step(_sum_reducer),
        reducer_node_id="0.reducer",
        initial=0,
    )
    assert captured_fold_kwargs["allowed_callable_modules"] == ("custom.driver.mod", __name__)

    # Also verify submit_step propagates allowed_callable_modules when remote supports it.
    import django_ray.runtime.remote as remote_module

    def _fake_remote_step_sig(
        *_args: Any,
        allowed_callable_modules: tuple[str, ...] | None = None,
        **_kwargs: Any,
    ) -> Any:
        return allowed_callable_modules

    monkeypatch.setattr(remote_module, "execute_workflow_step_remote", _fake_remote_step_sig)
    captured_step_kwargs: dict[str, Any] = {}

    class _FakeRemoteStep:
        @staticmethod
        def options(**_opts: Any) -> Any:
            class _Remote:
                @staticmethod
                def remote(*_args: Any, **kwargs: Any) -> str:
                    captured_step_kwargs.update(kwargs)
                    return "ref-1"

            return _Remote()

    executor.remote_step = _FakeRemoteStep()
    executor.progress_actor = None
    executor.task_execution_pk = None
    executor.workflow_run_identity = None
    executor.reporting_policy = "disabled"
    executor.workflow_progress_limits = None
    executor._pending_leaf_outcomes = {}
    executor._progress_suppression_depth = 0
    executor._cleanup_capture_stack = []
    executor.submit_step(step(_add_one), (1,), {}, "0", ())
    assert captured_step_kwargs["allowed_callable_modules"] == ("custom.driver.mod", __name__)


# ---------------------------------------------------------------------------
# R2-019: Consistent allowlist-enforced workflow callable resolution (5 sites)
# ---------------------------------------------------------------------------


def test_workflow_callable_resolution_enforces_allowlist_across_all_five_sites(
    settings: Any,
) -> None:
    settings.DJANGO_RAY = {
        "RAY_ADDRESS": "auto",
        "ALLOWED_CALLABLE_MODULES": ["testproject"],
    }
    disallowed_path = f"{__name__}._sum_reducer"

    # 1. Local executor start_result_fold
    local_exec = workflows_module._LocalExecutor()
    with pytest.raises(ImportError, match="not permitted"):
        local_exec.start_result_fold(
            max_items=4,
            max_concurrency=2,
            max_serialized_bytes=4096,
            actor_options={"num_cpus": 0.25, "memory": 8192},
            reducer=step(disallowed_path),
            reducer_node_id="0.reducer",
            initial=0,
        )

    # 2. Local executor reduce_local
    with pytest.raises(ImportError, match="not permitted"):
        local_exec.reduce_local(step(disallowed_path), 0, 1)

    # 3. Map.reduce eager validation skips ImportError gracefully without importing disallowed module
    mapped = map_step(step(f"{__name__}._add_one")).with_limits(max_concurrency=2, max_items=4)
    reduced = mapped.reduce(
        step(disallowed_path),
        initial=0,
        max_serialized_bytes=4096,
        actor_options={"num_cpus": 0.25, "memory": 8192},
    )
    assert reduced.result_fold is not None

    # 4. workflow/plans.py _callable_code_identity treats unpermitted module as worker_runtime_only
    identity = _callable_code_identity(disallowed_path, module_cache={})
    assert identity["deployment"] == "worker_runtime_only"
    assert identity["stable"] is False

    # 5. workflow/strategy.py build_fixed_actor_graph_plan rejects unpermitted callable
    sig = step(f"{__name__}._add_one", django=False)
    plan = materialize_workflow_plan(
        sig,
        build_context=WorkflowPlanBuildContext(build_revision="git:" + "0" * 40),
    )
    fixed_plan, rejections = build_fixed_actor_graph_plan(sig, plan)
    assert fixed_plan is None
    assert any(r.code == "UNRESOLVED_CODE_IDENTITY" for r in rejections)


# ---------------------------------------------------------------------------
# R2-005 & R2-030: RayCoreRunner runner_family, @ray_task contract & no live re-hash
# ---------------------------------------------------------------------------


def test_ray_core_runner_family_contract_options_and_no_post_snapshot_rehash(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "pkg"
    working_dir.mkdir()
    code_file = working_dir / "mod.py"
    code_file.write_text("VALUE = 1\n", encoding="utf-8")

    settings.DJANGO_RAY = {
        "RAY_ADDRESS": "auto",
        "ALLOWED_CALLABLE_MODULES": [__name__],
        "RUNTIME_ENV_PROFILES": {
            "ml-gpu": {"working_dir": str(working_dir)},
        },
    }

    captured_options: list[dict[str, Any]] = []

    class _FakeRef:
        @staticmethod
        def hex() -> str:
            return "a" * 56

    class _FakeRemoteFn:
        def options(self, **opts: Any) -> _FakeRemoteFn:
            captured_options.append(opts)
            return self

        @staticmethod
        def remote(*_args: Any, **_kwargs: Any) -> _FakeRef:
            return _FakeRef()

    fake_ray = SimpleNamespace(
        is_initialized=lambda: True,
        remote=lambda fn: _FakeRemoteFn(),
        get_runtime_context=lambda: SimpleNamespace(get_job_id=lambda: "02000000"),
        util=SimpleNamespace(
            client=SimpleNamespace(ray=SimpleNamespace(is_connected=lambda: False))
        ),
        cloudpickle=None,
    )
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    monkeypatch.setattr("django_ray.runner.ray_core._execute_django_task_remote_cached", None)

    # Mutate live disk right after snapshot capture to verify no post-snapshot live re-hash fails.
    import django_ray.runtime.runtime_env as runtime_env_mod

    seen_snapshot_envs: list[Any] = []

    def _mutate_after_snapshot(env: Any) -> dict[str, Any]:
        seen_snapshot_envs.append(env)
        # Mutate live working_dir on disk after snapshot capture; RayCoreRunner must not re-hash live disk.
        code_file.write_text("VALUE = 2\n", encoding="utf-8")
        return {"working_dir": "gcs://immutable-snapshot"}

    monkeypatch.setattr(runtime_env_mod, "prepare_runtime_env_for_ray_core", _mutate_after_snapshot)

    runner = RayCoreRunner()
    assert runner.runner_family == "ray_core"

    task_exec = SimpleNamespace(
        pk=77,
        task_id="contract-gpu-77",
        attempt_number=1,
        execution_generation=0,
        execution_protocol_version=1,
        callable_path=f"{__name__}._contract_gpu_task",
        args_json="[5]",
        kwargs_json="{}",
        input_reference=None,
        runtime_env_profile=None,
        runtime_env_json="{}",
        runtime_env_hash="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    )
    runner.submit_durable(task_exec)  # type: ignore[arg-type]

    assert len(captured_options) == 1
    opts = captured_options[0]
    assert opts["num_cpus"] == 2
    assert opts["num_gpus"] == 1
    assert opts["memory"] == 512 * 1024 * 1024
    assert "runtime_env" in opts


# ---------------------------------------------------------------------------
# R2-008: Atomic purge + reactivation restoration & concurrent serialization
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_purged_tombstone_reactivation_restores_deleted_payload_bytes(
    tmp_path: Path,
) -> None:
    config = {
        "MAX_INLINE_INPUT_SIZE_BYTES": 0,
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
    }
    prepared = prepare_task_input(["hello", "world"], {"x": 1}, config)
    assert prepared.input_reference is not None
    payload = register_task_input(prepared, config)
    assert payload is not None

    # Simulate a completed purge that removed external bytes and left a PURGED tombstone.
    input_storage.delete_input_reference(prepared.input_reference, config=config)
    TaskInputPayload.objects.filter(pk=payload.pk).update(
        state=InputPayloadState.PURGED,
        purged_at=timezone.now(),
        cleanup_error="",
    )

    recovered = register_task_input(prepared, config, verify_storage=False)
    assert recovered is not None
    assert recovered.state == InputPayloadState.ACTIVE
    assert recovered.purged_at is None
    assert recovered.cleanup_error == ""
    assert load_task_input(
        args_json="null",
        kwargs_json="null",
        input_reference=prepared.input_reference,
        config=config,
    ) == (["hello", "world"], {"x": 1})


@pytest.mark.django_db(transaction=True)
def test_concurrent_purge_and_register_toctou_never_deletes_live_input(
    tmp_path: Path,
    settings: Any,
) -> None:
    config = {
        "MAX_INLINE_INPUT_SIZE_BYTES": 0,
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
    }
    settings.DJANGO_RAY = {"RAY_ADDRESS": "auto", **config}
    prepared = prepare_task_input(["concurrent-payload"], {"k": "v"}, config)
    assert prepared.input_reference is not None
    payload = register_task_input(prepared, config)
    assert payload is not None

    # Unattached/freshly registered input is protected by the 60s grace floor even at cutoff=now.
    cmd = PurgeInputsCommand()
    assert (
        cmd._process_reference(
            prepared.input_reference,
            cutoff=timezone.now(),
            delete=True,
        )
        == "skipped"
    )

    old_ts = timezone.now() - timedelta(days=60)
    TaskInputPayload.objects.filter(pk=payload.pk).update(created_at=old_ts, last_used_at=old_ts)
    RayTaskExecution.objects.create(
        task_id="old-purge-task",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.SUCCEEDED,
        created_at=old_ts,
        finished_at=old_ts,
        input_reference=prepared.input_reference,
    )

    # Atomic purge deletes the external blob and commits a PURGED tombstone.
    outcome = cmd._process_reference(
        prepared.input_reference,
        cutoff=timezone.now() - timedelta(days=30),
        delete=True,
    )
    assert outcome == "purged"
    payload.refresh_from_db()
    assert payload.state == InputPayloadState.PURGED

    # Re-registering the same content-addressed input restores the blob and reactivates safely.
    reactivated = register_task_input(prepared, config)
    assert reactivated is not None
    assert reactivated.state == InputPayloadState.ACTIVE
    assert reactivated.cleanup_error == ""
    loaded_args, loaded_kwargs = load_task_input(
        args_json="null",
        kwargs_json="null",
        input_reference=prepared.input_reference,
        config=config,
    )
    assert loaded_args == ["concurrent-payload"]
    assert loaded_kwargs == {"k": "v"}


# ---------------------------------------------------------------------------
# R2-012: Workflow DSL 2.0 Ray parallelism, RayTaskError, partial, allowlist, durable
# ---------------------------------------------------------------------------


def test_workflow_signature_operators_explain_and_reexports(settings: Any) -> None:
    settings.DJANGO_RAY = {"RAY_ADDRESS": "auto", "ALLOWED_CALLABLE_MODULES": [__name__]}

    direct_pipe = step(_add_one) | step(_double)
    assert isinstance(direct_pipe, Chain)
    assert direct_pipe.run(4, use_ray=False) == 10

    direct_group = step(_double) & step(_triple)
    assert isinstance(direct_group, Group)
    assert direct_group.run(4, use_ray=False) == [8, 12]

    explained = (step(_add_one) | (step(_double) & step(_triple))).explain(format="dict")
    assert explained["root_kind"] == "chain"
    assert explained["summary"]["group_count"] == 1

    # Re-exports exist directly on django_ray.workflows.
    assert workflows_module.WorkflowExpr is WorkflowExpr
    assert workflows_module.pipe is pipe
    assert workflows_module.parallel is parallel
    assert workflows_module.branch is branch
    assert workflows_module.explain_workflow is explain_workflow


def test_dsl_branch_rejects_partial_and_enforces_allowlist_for_callable_objects(
    settings: Any,
) -> None:
    partial_pred = functools.partial(_is_even)
    with pytest.raises(WorkflowDefinitionError, match="module-level callable"):
        branch(partial_pred, on_true=_double, on_false=_triple)

    # Even when passing a live function object (not a string), allowlist is enforced.
    settings.DJANGO_RAY = {"RAY_ADDRESS": "auto", "ALLOWED_CALLABLE_MODULES": ["testproject"]}
    expr = branch(_is_even, on_true=_double, on_false=_triple)
    with pytest.raises(ImportError, match="not permitted"):
        expr.run(4, use_ray=False)


def test_dsl_fallback_unwraps_ray_task_error_cause(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    settings.DJANGO_RAY = {"RAY_ADDRESS": "auto", "ALLOWED_CALLABLE_MODULES": [__name__]}

    class _FakeRayExec(workflows_module._LocalExecutor):
        def submit_step(
            self,
            signature: Any,
            input_args: tuple[Any, ...],
            input_kwargs: dict[str, Any],
            node_id: str,
            dependencies: tuple[str, ...],
        ) -> Any:
            try:
                return super().submit_step(
                    signature, input_args, input_kwargs, node_id, dependencies
                )
            except Exception as err:
                raise RayTaskError(err) from err

    monkeypatch.setattr(workflows_module, "_provide_workflow_executor", lambda _u: _FakeRayExec())
    monkeypatch.setattr(workflows_module, "_check_is_local_workflow_executor", lambda _e: False)
    from django_ray.workflow import _types as workflow_types

    monkeypatch.setattr(workflow_types, "_get_executor_provider", lambda _u: _FakeRayExec())
    monkeypatch.setattr(workflow_types, "_is_local_executor_provider", lambda _e: False)

    expr = WorkflowExpr(_fail_value_error).with_fallback(
        _recover_with_error,
        exceptions=(ValueError,),
        pass_error=True,
    )
    assert expr.run(7, use_ray=True) == "recovered:7:ValueError:bad:7"

    # Non-matching wrapped error propagates unchanged.
    narrow = WorkflowExpr(_fail_value_error).with_fallback(
        _double,
        exceptions=(KeyError,),
    )
    with pytest.raises(RayTaskError):
        narrow.run(7, use_ray=True)


def test_dsl_group_and_map_execute_concurrently_on_ray_honoring_bounds_and_durable_context(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    settings.DJANGO_RAY = {"RAY_ADDRESS": "auto", "ALLOWED_CALLABLE_MODULES": [__name__]}

    class _TrackingRayExecutor(workflows_module._Executor):
        def __init__(self) -> None:
            self.in_flight = 0
            self.peak_in_flight = 0
            self.cancel_timeouts: list[float] = []
            self.submitted_before_resolve = 0
            self.resolved_any = False

        def submit_step(
            self,
            signature: Any,
            input_args: tuple[Any, ...],
            input_kwargs: dict[str, Any],
            node_id: str,
            dependencies: tuple[str, ...],
        ) -> Any:
            del node_id, dependencies
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            if not self.resolved_any:
                self.submitted_before_resolve += 1
            fn = import_utils.resolve_workflow_callable(signature.callable_path)
            return ("ref", fn, input_args, input_kwargs)

        def collect(self, values: list[Any]) -> Any:
            return ("collect", values)

        def wait_one(self, values: Any) -> int:
            return 0

        def resolve(self, value: Any) -> Any:
            self.resolved_any = True
            if isinstance(value, tuple) and value[0] == "collect":
                return [self.resolve(item) for item in value[1]]
            if isinstance(value, tuple) and value[0] == "ref":
                self.in_flight = max(0, self.in_flight - 1)
                _, fn, args, kwargs = value
                return fn(*args, **kwargs)
            return value

        def cancel_and_drain_fold_payloads(self, values: Any, *, timeout_seconds: float) -> None:
            del values
            self.cancel_timeouts.append(timeout_seconds)

    from django_ray.workflow import _types as workflow_types

    # 1. Parallel conditional Group submits both branches before resolving either.
    group_exec = _TrackingRayExecutor()
    monkeypatch.setattr(workflow_types, "_get_executor_provider", lambda _u: group_exec)
    monkeypatch.setattr(workflow_types, "_is_local_executor_provider", lambda _e: False)

    par_expr = parallel(branch(_is_even, on_true=_double, on_false=_triple), _add_one)
    assert par_expr.run(4, use_ray=True) == [8, 5]
    assert group_exec.submitted_before_resolve == 2

    # 2. Conditional Map honors max_concurrency sliding window and cancel_timeout_seconds.
    map_exec = _TrackingRayExecutor()
    monkeypatch.setattr(workflow_types, "_get_executor_provider", lambda _u: map_exec)
    cond_body = branch(_is_even, on_true=_double, on_false=_triple)
    map_expr = WorkflowExpr(step(f"{__name__}._add_one")).map(
        cond_body,
        max_concurrency=3,
        max_items=10,
        cancel_timeout_seconds=2.25,
    )
    # Execute just the _DslMapNode directly over 6 items.
    map_node: Any = cast(Any, map_expr.node).children[1]
    direct_map = WorkflowExpr._from_node(map_node)
    assert direct_map.run([1, 2, 3, 4, 5, 6], use_ray=True) == [3, 4, 9, 8, 15, 12]
    assert map_exec.peak_in_flight == 3

    # Failing conditional map forwards cancel_timeout_seconds to executor cleanup.
    fail_exec = _TrackingRayExecutor()
    monkeypatch.setattr(workflow_types, "_get_executor_provider", lambda _u: fail_exec)
    fail_body = branch(_is_even, on_true=_fail_value_error, on_false=_triple)
    fail_map_expr = WorkflowExpr(step(f"{__name__}._add_one")).map(
        fail_body,
        max_concurrency=3,
        cancel_timeout_seconds=3.5,
    )
    fail_direct_map = WorkflowExpr._from_node(cast(Any, fail_map_expr.node).children[1])
    with pytest.raises(ValueError, match="bad:2"):
        fail_direct_map.run([1, 2, 3, 4], use_ray=True)
    assert fail_exec.cancel_timeouts == [3.5]

    # 3. Conditional DSL chain runs inside durable_task_execution without WorkflowPlanMismatchError.
    local_exec = workflows_module._LocalExecutor()
    monkeypatch.setattr(workflow_types, "_get_executor_provider", lambda _u: local_exec)
    monkeypatch.setattr(workflow_types, "_is_local_executor_provider", lambda _e: True)
    durable_expr = (
        WorkflowExpr(_add_one) | branch(_is_even, on_true=_double, on_false=_triple) | _add_one
    )
    with durable_task_execution(
        999,
        task_id="durable-dsl-task",
        attempt_number=1,
        execution_generation=0,
    ):
        assert durable_expr.run(3, use_ray=False) == 9


def test_dsl_parallel_direct_group_cancels_partial_branch_refs_on_submit_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial chain refs captured before a later step raises during submit must be cancelled."""
    from contextlib import contextmanager

    from django_ray.workflow import dsl as dsl_module
    from django_ray.workflows import Chain, Step

    cancelled_batches: list[list[Any]] = []
    first_ref = object()

    class _PartialFailExecutor:
        def __init__(self) -> None:
            self._cleanup_stack: list[list[Any]] = []
            self.submit_calls = 0

        @contextmanager
        def capture_cleanup(self, bucket: list[Any]):
            self._cleanup_stack.append(bucket)
            try:
                yield bucket
            finally:
                self._cleanup_stack.pop()

        def track_cleanup(self, value: Any) -> None:
            if self._cleanup_stack:
                self._cleanup_stack[-1].append(value)

        def submit_step(self, *_args: Any, **_kwargs: Any) -> Any:
            self.submit_calls += 1
            if self.submit_calls == 1:
                return first_ref
            raise RuntimeError("second chain step submit failed")

        def cancel_and_drain_fold_payloads(
            self,
            refs: list[Any],
            *,
            timeout_seconds: float,
        ) -> None:
            del timeout_seconds
            cancelled_batches.append(list(refs))

    failing_chain = Chain((Step(f"{__name__}._add_one"), Step(f"{__name__}._double")))
    group_expr = WorkflowExpr(failing_chain) & branch(_is_even, on_true=_double, on_false=_triple)
    executor = _PartialFailExecutor()
    with pytest.raises(RuntimeError, match="second chain step submit failed"):
        dsl_module._execute_dsl_group_concurrent(
            cast(Any, group_expr.node),
            (2,),
            {},
            executor=executor,
            node_id="0",
            cancel_timeout_seconds=1.0,
        )

    assert cancelled_batches == [[first_ref]]


def test_group_and_unbounded_map_cancel_partial_chain_refs_on_submit_failure() -> None:
    """Codex P2: Group._submit and unbounded Map._submit must preserve partial chain refs."""
    from contextlib import contextmanager

    from django_ray.workflows import Chain, Group, Map, Step

    class _PartialSubmitExecutor:
        def __init__(self) -> None:
            self._cleanup_stack: list[list[Any]] = []
            self.submit_calls = 0
            self.first_ref = object()
            self.fold_cancelled: list[list[Any]] = []
            self.drained_cancelled: list[list[Any]] = []

        @contextmanager
        def capture_cleanup(self, bucket: list[Any]):
            self._cleanup_stack.append(bucket)
            try:
                yield bucket
            finally:
                self._cleanup_stack.pop()

        @contextmanager
        def suppress_progress(self):
            yield

        def track_cleanup(self, value: Any) -> None:
            if self._cleanup_stack:
                self._cleanup_stack[-1].append(value)

        def map_started(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def map_progress(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def map_finished(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def submit_step(self, *_args: Any, **_kwargs: Any) -> Any:
            self.submit_calls += 1
            if self.submit_calls == 1:
                return self.first_ref
            raise RuntimeError("second chain step submit failed")

        def cancel_and_drain_fold_payloads(
            self,
            refs: list[Any],
            *,
            timeout_seconds: float,
        ) -> None:
            del timeout_seconds
            self.fold_cancelled.append(list(refs))

        def cancel_and_drain(
            self,
            refs: list[Any],
            *,
            timeout_seconds: float,
        ) -> None:
            del timeout_seconds
            self.drained_cancelled.append(list(refs))

    # 1. Bounded map containing a Group whose Chain partially submits and raises.
    failing_chain = Chain((Step(f"{__name__}._add_one"), Step(f"{__name__}._double")))
    group_sig = Group((failing_chain, Step(f"{__name__}._double")))
    bounded_map = Map(group_sig, max_concurrency=2, max_items=4)
    exec_bounded = _PartialSubmitExecutor()
    with pytest.raises(RuntimeError, match="second chain step submit failed"):
        bounded_map._submit(cast(Any, exec_bounded), ([1, 2],), {}, "0", ())
    assert exec_bounded.fold_cancelled == [[exec_bounded.first_ref]]
    assert exec_bounded.drained_cancelled == [[exec_bounded.first_ref]]

    # 2. Unbounded map whose Chain partially submits and raises inside an outer capture.
    unbounded_map = Map(failing_chain)
    exec_unbounded = _PartialSubmitExecutor()
    outer_capture: list[Any] = []
    with pytest.raises(RuntimeError, match="second chain step submit failed"):
        with exec_unbounded.capture_cleanup(outer_capture):
            unbounded_map._submit(cast(Any, exec_unbounded), ([1, 2],), {}, "0", ())
    assert exec_unbounded.fold_cancelled == [[exec_unbounded.first_ref]]
    assert outer_capture == [exec_unbounded.first_ref]


def test_resolved_cleanup_refs_resist_python_id_reuse_and_support_weakref_and_sentinel() -> None:
    """Codex P2: resolved ref tracking uses stable Ray ObjectIDs and GC-safe identity checks."""
    import gc
    import weakref

    from django_ray.workflows import _LocalExecutor

    executor = _LocalExecutor()

    # Empty resolved tracking returns all values verbatim.
    sentinel_a = object()
    assert executor._unresolved_cleanup_refs([sentinel_a]) == [sentinel_a]

    # 1. Ray ObjectRef-compatible stubs use stable .hex() / .binary() keys immune to id() reuse.
    class ObjectRef:
        def __init__(
            self,
            *,
            hex_val: str | None = None,
            bin_val: bytes | None = None,
            raise_hex: bool = False,
            raise_bin: bool = False,
        ) -> None:
            self._hex = hex_val
            self._bin = bin_val
            self._raise_hex = raise_hex
            self._raise_bin = raise_bin

        def hex(self) -> Any:
            if self._raise_hex:
                raise RuntimeError("hex unavailable")
            return self._hex

        def binary(self) -> Any:
            if self._raise_bin:
                raise RuntimeError("binary unavailable")
            return self._bin

    ref_hex_1 = ObjectRef(hex_val="aa" * 28)
    ref_hex_2 = ObjectRef(hex_val="bb" * 28)
    ref_bin_1 = ObjectRef(raise_hex=True, bin_val=b"\x01\x02\x03")
    ref_bin_2 = ObjectRef(raise_hex=True, bin_val=b"\x04\x05\x06")
    ref_no_key = ObjectRef(hex_val="", raise_bin=True)

    executor.mark_resolved_cleanup(ref_hex_1)
    executor.mark_resolved_cleanup(ref_bin_1)
    # Distinct Python wrapper with the SAME ObjectID hex is still recognized as resolved.
    ref_hex_1_alias = ObjectRef(hex_val="aa" * 28)
    assert executor._unresolved_cleanup_refs(
        [ref_hex_1, ref_hex_1_alias, ref_hex_2, ref_bin_1, ref_bin_2, ref_no_key]
    ) == [ref_hex_2, ref_bin_2, ref_no_key]

    # 2. Weakrefable non-Ray refs evict on GC and never collide with a new object at the same id().
    class _WeakStub:
        pass

    weak_first = _WeakStub()
    executor.mark_resolved_cleanup(weak_first)
    assert executor._unresolved_cleanup_refs([weak_first]) == []
    del weak_first
    gc.collect()
    weak_second = _WeakStub()
    # Even if a stale weakref entry existed for id(weak_second), identity check rejects it.
    other_live = _WeakStub()
    executor._resolved_cleanup_weak[id(weak_second)] = weakref.ref(other_live)  # type: ignore[attr-defined]
    assert executor._unresolved_cleanup_refs([weak_second]) == [weak_second]

    # 3. Non-weakrefable sentinels (object()) fall back cleanly and remain identity-checked.
    sentinel_b = object()
    executor.mark_resolved_cleanup(sentinel_a)
    assert executor._unresolved_cleanup_refs([sentinel_a, sentinel_b]) == [sentinel_b]


async def _async_branch_predicate(value: int) -> bool:
    return value > 0


def _gen_branch_predicate(value: int):
    yield value > 0


async def _asyncgen_branch_predicate(value: int):
    yield value > 0


def _wrapper_returning_coroutine(value: int) -> Any:
    return _async_branch_predicate(value)


def _wrapper_returning_generator(value: int) -> Any:
    return (value > 0 for _ in range(1))


def test_dsl_branch_dotted_path_rejects_async_generator_and_deferred_predicates() -> None:
    """Codex P2: dotted-path branch predicates reject async/generator callables and deferred values."""
    from django_ray.runtime.import_utils import active_allowed_callable_modules

    with active_allowed_callable_modules([__name__]):
        for fn_name in (
            "_async_branch_predicate",
            "_gen_branch_predicate",
            "_asyncgen_branch_predicate",
        ):
            expr = branch(f"{__name__}.{fn_name}", on_true=_double, on_false=_triple)
            with pytest.raises(WorkflowDefinitionError, match="synchronous module-level callable"):
                expr.run(-1, use_ray=False)

        for fn_name in ("_wrapper_returning_coroutine", "_wrapper_returning_generator"):
            expr = branch(f"{__name__}.{fn_name}", on_true=_double, on_false=_triple)
            with pytest.raises(
                WorkflowDefinitionError,
                match="concrete synchronous value",
            ):
                expr.run(-1, use_ray=False)


# ---------------------------------------------------------------------------
# R2-014: Live TaskWorkerLease authority verification in ray_job_request_storage
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_ray_job_request_storage_verifies_live_worker_lease(tmp_path: Path) -> None:
    address = "http://ray-head:8265"
    execution = RayTaskExecution.objects.create(
        task_id="lease-fenced-rq2-task",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=1,
        execution_protocol_version=1,
        claimed_by_worker="worker-live",
        ray_address=address,
        args_json="[1]",
        kwargs_json="{}",
        runtime_env_profile=None,
        runtime_env_hash="a" * 64,
    )
    identity = ExecutionIdentity(
        task_execution_pk=int(execution.pk),
        task_id=execution.task_id,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
    )
    job_id = (
        f"{STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX}{coordination_sha256(identity)}"
    )
    execution.ray_job_id = job_id
    execution.save(update_fields=["ray_job_id"])
    handle = SubmissionHandle(
        ray_job_id=job_id, ray_address=address, submitted_at=datetime.now(UTC)
    )

    encoded = encode_execution_request(
        ExecutionRequest(
            identity=identity,
            execution_protocol_version=1,
            callable_path=execution.callable_path,
            transport_version=1,
            serialized_args=execution.args_json,
            serialized_kwargs=execution.kwargs_json,
            input_reference=None,
            runtime_env_profile=None,
            runtime_env_hash=execution.runtime_env_hash,
            runtime_env_plan_identity={},
            compiled_graph_submission_transport="ray-job",
        )
    )
    prepared = prepare_ray_job_request(
        encoded,
        {
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
        },
    )

    # Create an inactive/expired lease -> attach and release MUST reject.
    now = timezone.now()
    lease = TaskWorkerLease.objects.create(
        worker_id="worker-live",
        hostname="host-1",
        pid=1234,
        queue_name="default",
        is_active=False,
        stopped_at=now,
        last_heartbeat_at=now - timedelta(seconds=300),
        lease_expires_at=now - timedelta(seconds=200),
    )
    with pytest.raises(RayJobRequestStorageError) as caught:
        register_and_attach_ray_job_request(
            prepared,
            task_execution=execution,
            submission_handle=handle,
        )
    assert caught.value.classification is RayJobRequestStorageRejection.BINDING_MISMATCH
    assert (
        release_ray_job_request_reservation(
            execution,
            handle,
            expected_reference=None,
        )
        is False
    )

    # Activate lease with fresh DB-clock expiry -> attach and release succeed.
    TaskWorkerLease.objects.filter(pk=lease.pk).update(
        is_active=True,
        stopped_at=None,
        last_heartbeat_at=timezone.now(),
        lease_expires_at=timezone.now() + timedelta(seconds=120),
    )
    ref = register_and_attach_ray_job_request(
        prepared,
        task_execution=execution,
        submission_handle=handle,
        require_live_lease=True,
    )
    assert ref == prepared.reference
    assert (
        release_ray_job_request_reservation(
            execution,
            handle,
            expected_reference=ref,
            require_live_lease=True,
        )
        is True
    )


# ---------------------------------------------------------------------------
# R2-023: Non-unlinked persistent FileLock & payload_registry exclusion
# ---------------------------------------------------------------------------


def test_blob_storage_persistent_filelock_not_unlinked_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from filelock import Timeout as FileLockTimeout

    storage = FilesystemResultStorage(tmp_path)
    payload = '{"lock":"persistent"}'
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    target_dir = tmp_path / digest[:2] / digest[2:4]
    target_dir.mkdir(parents=True)
    lock_file = target_dir / ".django-ray-install.lock"
    lock_file.write_text("", encoding="utf-8")

    def _no_hardlinks(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EOPNOTSUPP, "hard links unsupported")

    class _TimingOutLock:
        def __init__(self, path: str, timeout: float) -> None:
            self.path = path
            self.timeout = timeout

        def __enter__(self) -> None:
            raise FileLockTimeout(self.path)

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(result_storage.os, "link", _no_hardlinks)
    import django_ray._blob_storage as blob_storage_mod

    monkeypatch.setattr(blob_storage_mod, "FileLock", _TimingOutLock)

    with pytest.raises(ResultStorageError, match="Failed to persist result payload"):
        storage.store(serialized_result=payload)

    # Direct atomic install call also raises OSError on FileLockTimeout without unlinking lock_file.
    tmp_blob = target_dir / "temp.tmp"
    tmp_blob.write_text(payload, encoding="utf-8")
    with pytest.raises(OSError, match="lock is unavailable"):
        blob_storage_mod.install_temporary_payload_atomic(
            tmp_blob,
            target_dir / f"{digest}.json",
            os_module=SimpleNamespace(link=_no_hardlinks, replace=os.replace),
        )

    # Lock file MUST NOT have been unlinked on FileLockTimeout!
    assert lock_file.exists()
    assert payload_registry._is_temporary_filesystem_entry(
        ".django-ray-install.lock",
        is_dir=False,
    )


# ---------------------------------------------------------------------------
# R2-024: ResultFold dict/list rollback, dict expansion sizing, append_batch wiring
# ---------------------------------------------------------------------------


def test_result_fold_dict_and_list_rollback_and_dict_value_expansion_sizing() -> None:
    # 1. Primitive dict rollback on in-place overwrite + key insertion + exception.
    dict_fold = WorkflowMapResultFold(
        4, 4, 8192, f"{__name__}._dict_mutate_and_fail", False, (), {}, {}
    )
    dict_fold.append(0, 0)
    with pytest.raises(RuntimeError, match="dict-boom"):
        dict_fold.append(1, 1)
    assert dict_fold.folded_items == 1
    assert dict_fold._accumulator == {"k0": 0}

    # 2. Nested mutable dict rollback restores mutated inner list.
    nested_fold = WorkflowMapResultFold(
        4, 4, 8192, f"{__name__}._nested_dict_mutate_and_fail", False, (), {}, {"items": []}
    )
    nested_fold.append(0, 0)
    with pytest.raises(RuntimeError, match="nested-boom"):
        nested_fold.append(1, 1)
    assert nested_fold.folded_items == 1
    assert nested_fold._accumulator == {"items": [0]}

    # 3. List element overwrite rollback restores index 0.
    list_fold = WorkflowMapResultFold(
        4, 4, 8192, f"{__name__}._list_overwrite_and_fail", False, (), {}, []
    )
    list_fold.append(0, 42)
    with pytest.raises(RuntimeError, match="list-overwrite-boom"):
        list_fold.append(1, 1)
    assert list_fold.folded_items == 1
    assert list_fold._accumulator == [42]

    # 4. Dict value expansion on new key catches mid-fold overflow immediately.
    expand_fold = WorkflowMapResultFold(
        4, 4, 1024, f"{__name__}._dict_expand_value", False, (), {}, {}
    )
    with pytest.raises(ResultFoldOverflowError, match="accumulator serialization"):
        expand_fold.append(0, 0)
    assert expand_fold._accumulator == {}

    # 5. Dict value expansion on existing key overwrite catches mid-fold overflow immediately.
    overwrite_fold = WorkflowMapResultFold(
        4, 4, 1024, f"{__name__}._dict_overwrite_expand", False, (), {}, {}
    )
    overwrite_fold.append(0, 0)
    with pytest.raises(ResultFoldOverflowError, match="accumulator serialization"):
        overwrite_fold.append(1, 1)
    assert overwrite_fold._accumulator == {"slot": "Y" * 4}


def test_workflows_map_wires_append_batch_for_result_buffer_and_fold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buf_actor = WorkflowMapResultBuffer(max_items=6, max_serialized_bytes=16384)
    fold_actor = WorkflowMapResultFold(6, 6, 16384, f"{__name__}._sum_reducer", False, (), {}, 0)
    batch_calls = {"buffer": 0, "fold": 0}

    class _RemoteMethod:
        def __init__(self, fn: Any, counter_key: str | None = None) -> None:
            self._fn = fn
            self._counter_key = counter_key

        def options(self, **_kwargs: Any) -> _RemoteMethod:
            return self

        def remote(self, *args: Any, **kwargs: Any) -> Any:
            if self._counter_key is not None:
                batch_calls[self._counter_key] += 1
            return self._fn(*args, **kwargs)

    class _ActorProxy:
        def __init__(self, target: Any, counter_key: str) -> None:
            self.ready = _RemoteMethod(target.ready)
            self.append = _RemoteMethod(target.append)
            self.append_batch = _RemoteMethod(target.append_batch, counter_key=counter_key)
            self.finalize = _RemoteMethod(target.finalize)
            self.discard = _RemoteMethod(target.discard)

    class _BatchFakeRay:
        @staticmethod
        def wait(
            refs: list[Any],
            *,
            num_returns: int = 1,
            timeout: float | None = None,
            fetch_local: bool = True,
        ) -> tuple[list[Any], list[Any]]:
            del timeout, fetch_local
            return refs[:num_returns], refs[num_returns:]

        @staticmethod
        def get(ref: Any) -> Any:
            return ref

        @staticmethod
        def kill(*_args: Any, **_kwargs: Any) -> None:
            return None

    class _BatchRayExec(workflows_module._RayExecutor):
        def __init__(self) -> None:
            self.ray = _BatchFakeRay()
            self.task_context = None
            self.materialized_plan = None
            self.progress_actor = None
            self.workflow_run_identity = None
            self.reporting_policy = "disabled"
            self._progress_suppression_depth = 0
            self._cleanup_capture_stack = []
            self._pending_leaf_outcomes = {}

        def submit_step(
            self,
            signature: Any,
            input_args: tuple[Any, ...],
            input_kwargs: dict[str, Any],
            node_id: str,
            dependencies: tuple[str, ...],
        ) -> Any:
            del node_id, dependencies
            fn = import_utils.resolve_workflow_callable(signature.callable_path)
            return fn(*input_args, **input_kwargs)

        def resolve(self, value: Any) -> Any:
            return value

        def start_result_buffer(self, **_kwargs: Any) -> Any:
            return workflows_module._RayResultBufferSession(actor=_ActorProxy(buf_actor, "buffer"))

        def start_result_fold(self, **_kwargs: Any) -> Any:
            return workflows_module._RayResultFoldSession(
                actor=_ActorProxy(fold_actor, "fold"),
                maximum_items=6,
                maximum_concurrency=6,
                maximum_out_of_order_items=5,
                maximum_serialized_bytes=16384,
            )

    monkeypatch.setattr(workflows_module, "_get_executor", lambda _u, **_kw: _BatchRayExec())

    buf_wf = (
        map_step(step(_double))
        .with_limits(max_concurrency=6, max_items=6)
        .with_result_buffer(
            max_serialized_bytes=16384,
            actor_options={"num_cpus": 0.25, "memory": 32768},
        )
    )
    assert buf_wf.run([1, 2, 3, 4], use_ray=True) == [2, 4, 6, 8]
    assert batch_calls["buffer"] == 1

    fold_wf = (
        map_step(step(_double))
        .with_limits(max_concurrency=6, max_items=6)
        .reduce(
            step(_sum_reducer),
            initial=0,
            max_serialized_bytes=16384,
            actor_options={"num_cpus": 0.25, "memory": 32768},
        )
    )
    assert fold_wf.run([1, 2, 3, 4], use_ray=True) == 20
    assert batch_calls["fold"] == 1


# ---------------------------------------------------------------------------
# R2-026 & R2-027: Batched inventory lookups, single inline serialize, realpath
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_payload_registry_inventory_batches_db_lookups(tmp_path: Path) -> None:
    storage = FilesystemResultStorage(tmp_path)
    config = {
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
    }
    for index in range(15):
        ref = storage.store_payload(
            serialized_payload=json.dumps(
                {
                    "args": [index],
                    "kwargs": {},
                    "schema": "django-ray.task-input",
                    "version": 1,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        if index < 5:
            TaskInputPayload.objects.create(
                reference=ref,
                payload_kind=InputPayloadKind.TASK_INPUT,
                backend="filesystem",
                digest=ref.split("sha256/")[1].split("?")[0],
                size_bytes=int(ref.split("bytes=")[1]),
                envelope_version=1,
                state=InputPayloadState.ACTIVE,
                last_used_at=timezone.now(),
            )

    with CaptureQueriesContext(connection) as captured:
        report = payload_registry.discover_and_reap_unregistered_payloads(
            config=config,
            max_objects=50,
            delete_unregistered=False,
        )

    assert report.counts["scanned"] == 15
    assert report.counts["registered_active"] == 5
    # Batched: 1 TaskInputPayload IN query + 1 RayTaskExecution IN query + 1 missing check = 3!
    assert len(captured) <= 4


def test_input_storage_inline_serializes_once_and_result_storage_uses_realpath(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dumps_count = 0
    original_dumps = json.dumps

    def _counting_dumps(*args: Any, **kwargs: Any) -> str:
        nonlocal dumps_count
        dumps_count += 1
        return original_dumps(*args, **kwargs)

    monkeypatch.setattr(input_storage.json, "dumps", _counting_dumps)
    prepared = prepare_task_input(
        [1, 2, 3],
        {"a": "b"},
        {"MAX_INLINE_INPUT_SIZE_BYTES": 4096},
    )
    assert prepared.is_external is False
    assert json.loads(prepared.args_json) == [1, 2, 3]
    assert json.loads(prepared.kwargs_json) == {"a": "b"}
    # Serialized args once + kwargs once (2 calls total, not double-serialized).
    assert dumps_count == 2

    # Verify validate_storage_configuration uses realpath to reject symlinked overlaps.
    real_dir = tmp_path / "shared_store"
    real_dir.mkdir()
    link_dir = tmp_path / "symlink_store"
    try:
        os.symlink(real_dir, link_dir, target_is_directory=True)
    except OSError:
        # Windows without symlink privileges: verify normpath_fn is os.path.realpath directly.
        seen_normpaths: list[Any] = []
        orig_overlap = result_storage.filesystem_namespaces_overlap

        def _spy_overlap(left: str, right: str, *, normpath_fn: Any = os.path.realpath) -> bool:
            seen_normpaths.append(normpath_fn)
            return orig_overlap(left, right, normpath_fn=normpath_fn)

        monkeypatch.setattr(result_storage, "filesystem_namespaces_overlap", _spy_overlap)
        validate_storage_configuration(
            {
                "RESULT_STORAGE_BACKEND": "filesystem",
                "RESULT_STORAGE_FILESYSTEM_PATH": str(tmp_path / "res"),
                "INPUT_STORAGE_BACKEND": "filesystem",
                "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path / "inp"),
            }
        )
        assert seen_normpaths == [os.path.realpath]
    else:
        with pytest.raises(ResultStorageError, match="result storage namespace"):
            validate_storage_configuration(
                {
                    "RESULT_STORAGE_BACKEND": "filesystem",
                    "RESULT_STORAGE_FILESYSTEM_PATH": str(real_dir),
                    "INPUT_STORAGE_BACKEND": "filesystem",
                    "INPUT_STORAGE_FILESYSTEM_PATH": str(link_dir),
                }
            )


# ---------------------------------------------------------------------------
# R2-030 & R2-038: CompiledGraph lifecycle, strategy, checkpoints, drain_status,
# reconnect-safe caches, failure DAG preservation, batched page re-stage
# ---------------------------------------------------------------------------


def test_compiled_graph_lifecycle_owner_lost_and_post_outer_teardown() -> None:
    session_id = WorkflowRunIdentity(
        task_execution_pk=1,
        attempt_number=1,
        execution_generation=0,
        run_id="cg-run-1",
    )
    inv_id = WorkflowInvocationIdentity(run_identity=session_id, invocation_id="inv-1")
    deadlines = LifecycleDeadlines(
        outer=10.0,
        admission=5.0,
        submission=6.0,
        result=8.0,
        cancellation=12.0,
        drain=15.0,
        teardown=20.0,
    )

    def _step_ok(state: Any, event: LifecycleEvent) -> LifecycleTransition:
        res = reduce_lifecycle(state, event)
        assert isinstance(res, LifecycleTransition), res
        return res

    # Prepare session to READY -> ADMITTED -> pre-submission CANCELLING -> OWNER_LOST.
    state = initial_session(session_id, deadlines=deadlines)
    t = _step_ok(
        state, LifecycleEvent(LifecycleEventKind.VALIDATION_REQUESTED, session_id, observed_at=1.0)
    )
    t = _step_ok(
        t.state,
        LifecycleEvent(
            LifecycleEventKind.VALIDATED,
            session_id,
            observed_at=1.1,
            action_token=t.actions[0].token,
        ),
    )
    t = _step_ok(
        t.state,
        LifecycleEvent(LifecycleEventKind.PREPARATION_REQUESTED, session_id, observed_at=1.2),
    )
    t = _step_ok(
        t.state,
        LifecycleEvent(
            LifecycleEventKind.PREPARED,
            session_id,
            observed_at=1.3,
            action_token=t.actions[0].token,
        ),
    )
    t = _step_ok(
        t.state,
        LifecycleEvent(
            LifecycleEventKind.ADMISSION_REQUESTED,
            session_id,
            observed_at=1.4,
            invocation_identity=inv_id,
            declared_outputs=1,
        ),
    )
    t = _step_ok(
        t.state,
        LifecycleEvent(
            LifecycleEventKind.ADMITTED,
            session_id,
            observed_at=1.5,
            invocation_identity=inv_id,
            action_token=t.actions[0].token,
        ),
    )
    ready_admitted = t.state

    # Case A: Pre-submission CANCELLING -> OWNER_LOST stays valid (NOT_STARTED + NOT_CREATED).
    cancelling = _step_ok(
        ready_admitted,
        LifecycleEvent(
            LifecycleEventKind.CANCELLATION_REQUESTED,
            session_id,
            observed_at=1.6,
            invocation_identity=inv_id,
        ),
    )
    assert cancelling.state.invocation_state is CompiledGraphInvocationState.CANCELLING
    lost = _step_ok(
        cancelling.state,
        LifecycleEvent(
            LifecycleEventKind.OWNER_LOST, session_id, observed_at=1.7, invocation_identity=inv_id
        ),
    )
    assert lost.state.primary_outcome is LifecycleOutcome.CANCELLED_PRE_SUBMISSION
    assert lost.state.effect_state is LifecycleEffectState.NOT_STARTED
    assert lost.state.session_state is CompiledGraphSessionState.QUARANTINED
    assert all(slot.state.value == "NOT_CREATED" for slot in lost.state.outputs)

    # Case B: Outer deadline expiry during RUNNING still emits DRAIN_INVOCATION and TEARDOWN!
    sub = _step_ok(
        ready_admitted,
        LifecycleEvent(
            LifecycleEventKind.SUBMISSION_REQUESTED,
            session_id,
            observed_at=2.0,
            invocation_identity=inv_id,
        ),
    )
    running = _step_ok(
        sub.state,
        LifecycleEvent(
            LifecycleEventKind.SUBMITTED,
            session_id,
            observed_at=2.1,
            invocation_identity=inv_id,
            action_token=sub.actions[0].token,
        ),
    )
    expired = _step_ok(
        running.state,
        LifecycleEvent(
            LifecycleEventKind.OUTER_DEADLINE_EXPIRED,
            session_id,
            observed_at=10.0,
            invocation_identity=inv_id,
        ),
    )
    assert len(expired.actions) == 1
    assert expired.actions[0].kind is LifecycleActionKind.DRAIN_INVOCATION
    assert expired.actions[0].deadline == 15.0

    drained = _step_ok(
        expired.state,
        LifecycleEvent(
            LifecycleEventKind.DRAINED,
            session_id,
            observed_at=11.0,
            invocation_identity=inv_id,
            action_token=expired.actions[0].token,
        ),
    )
    assert len(drained.actions) == 1
    assert drained.actions[0].kind is LifecycleActionKind.TEARDOWN
    assert drained.actions[0].deadline == 20.0


def test_in_process_fixed_actor_adapter_emits_application_error_and_strategy_alignment(
    settings: Any,
) -> None:
    settings.DJANGO_RAY = {"RAY_ADDRESS": "auto", "ALLOWED_CALLABLE_MODULES": [__name__]}
    sig = step(_fail_value_error, django=False)
    plan = materialize_workflow_plan(
        sig,
        build_context=WorkflowPlanBuildContext(build_revision="git:" + "a" * 40),
    )
    fixed_plan, rejections = build_fixed_actor_graph_plan(sig, plan)
    assert fixed_plan is not None and not rejections
    adapter = InProcessFixedActorAdapter(fixed_plan, clock=lambda: 1.0)
    session_id = WorkflowRunIdentity(1, 1, 0, "cg-app-err")
    inv_id = WorkflowInvocationIdentity(run_identity=session_id, invocation_id="inv-err")

    from django_ray.runtime.compiled_graph_lifecycle import LifecycleAction

    adapter.dispatch(LifecycleAction("tok-1", LifecycleActionKind.PREPARE, session_id, None, 10.0))
    adapter.bind_invocation_inputs((7,), {})
    event = adapter.dispatch(
        LifecycleAction("tok-2", LifecycleActionKind.SUBMIT, session_id, inv_id, 10.0)
    )
    assert event.kind is LifecycleEventKind.APPLICATION_ERROR
    assert isinstance(adapter.last_application_error, ValueError)

    # LocalWorkflowExecutionStrategy forwards selection.reporting_policy to bind_plan (not hardcoded).
    local_strategy = LocalWorkflowExecutionStrategy()
    local_plan = materialize_workflow_plan(step(_add_one))
    sel = local_strategy.validate(local_plan, requested_policy="local", reporting_policy="disabled")
    local_prep = local_strategy.prepare(local_plan, selection=sel)
    local_admitted = local_strategy.admit(local_prep)
    seen_reporting_policies: list[str] = []

    class _SpyLocalExecutor(workflows_module._LocalExecutor):
        def bind_plan(
            self,
            materialized_plan: Any,
            *,
            requested_policy: str,
            reporting_policy: str = "full",
        ) -> None:
            seen_reporting_policies.append(reporting_policy)
            super().bind_plan(
                materialized_plan,
                requested_policy=requested_policy,
                reporting_policy=reporting_policy,
            )

    from dataclasses import replace as dc_replace

    custom_sel = dc_replace(sel, reporting_policy="summary")
    custom_admitted = dc_replace(
        local_admitted,
        prepared=dc_replace(local_prep, selection=custom_sel),
    )
    import django_ray.workflow.strategy as strategy_mod

    original_create_local = strategy_mod.create_local_workflow_executor
    strategy_mod.create_local_workflow_executor = _SpyLocalExecutor  # type: ignore[assignment]
    try:
        local_handle = local_strategy.execute(custom_admitted, step(_add_one), (4,), {})
    finally:
        strategy_mod.create_local_workflow_executor = original_create_local
    assert local_strategy.consume_result(local_handle) == 5
    assert seen_reporting_policies == ["summary"]

    # DynamicTasksWorkflowExecutionStrategy aligns admitted.invocation_identity.run_identity.
    dyn_strategy = DynamicTasksWorkflowExecutionStrategy()
    dyn_sel = dyn_strategy.validate(local_plan, requested_policy="dynamic_tasks")
    prep = dyn_strategy.prepare(local_plan, selection=dyn_sel)
    admitted = dyn_strategy.admit(prep)
    allocated = WorkflowRunIdentity(10, 1, 0, "allocated-uuidv8")

    class _AllocatingExecutor(workflows_module._LocalExecutor):
        def bind_plan(self, *_args: Any, **_kwargs: Any) -> None:
            self.workflow_run_identity = allocated

    handle = dyn_strategy.execute(
        admitted, step(_add_one), (5,), {}, executor=_AllocatingExecutor()
    )
    assert handle.run_identity == allocated
    assert admitted.invocation_identity.run_identity == allocated


def test_checkpoints_allow_manual_retry_generation_and_reject_initial_attempt() -> None:
    planner = WorkflowCheckpointResumePlanner(
        workflow_plan_fingerprint="fp-w4",
        nodes=[CheckpointableNodeSpec("n1", "app.tasks.n1")],
    )
    cp = DurableStepCheckpointEnvelope(
        workflow_plan_fingerprint="fp-w4",
        node_id="n1",
        callable_path="app.tasks.n1",
        resumability_class=NodeResumabilityClass.PURE_DETERMINISTIC,
        side_effect_disposition=SideEffectDisposition.NONE,
        input_digest_sha256="sha256:" + "a" * 64,
        output_digest_sha256="sha256:" + "b" * 64,
        output_reference="resfs://sha256/out?bytes=8",
        source_attempt_number=1,
        source_execution_generation=0,
    )
    plan = planner.build_resume_plan(
        target_attempt_number=1,
        target_execution_generation=1,
        checkpoints=[cp],
    )
    assert plan.reused_node_ids == ("n1",)

    with pytest.raises(CheckpointFencingError):
        planner.build_resume_plan(
            target_attempt_number=1,
            target_execution_generation=0,
            checkpoints=[],
        )


def test_workflow_remote_cache_is_reconnect_safe_and_finish_progress_uses_drain_status(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    settings.DJANGO_RAY = {
        "RAY_ADDRESS": "auto",
        "WORKFLOW_PROGRESS_TERMINAL_FLUSH_TIMEOUT_SECONDS": 2.0,
    }
    reset_cached_workflow_remotes()
    session_state = {"job_id": "job-session-1"}
    remote_wrap_calls = 0

    class _SessionRay:
        @staticmethod
        def get_runtime_context() -> Any:
            return SimpleNamespace(get_job_id=lambda: session_state["job_id"])

        @staticmethod
        def remote(*args: Any, **kwargs: Any) -> Any:
            nonlocal remote_wrap_calls
            if args and callable(args[0]):
                remote_wrap_calls += 1
                return ("wrapped", session_state["job_id"], args[0])

            def _dec(fn: Any) -> Any:
                nonlocal remote_wrap_calls
                remote_wrap_calls += 1
                return ("wrapped", session_state["job_id"], fn, kwargs)

            return _dec

    monkeypatch.setitem(sys.modules, "ray", _SessionRay())
    first = workflows_module._get_cached_workflow_remotes()
    second = workflows_module._get_cached_workflow_remotes()
    assert first == second
    assert remote_wrap_calls == 3

    # Simulate reconnect -> new Ray job session invalidates stale wrappers automatically.
    session_state["job_id"] = "job-session-2"
    third = workflows_module._get_cached_workflow_remotes()
    assert third != first
    assert remote_wrap_calls == 6
    reset_cached_workflow_remotes()

    # Verify drain_status() RPC + failed sibling preservation + cleanup when published=False.
    drain_calls = 0
    snapshot_calls = 0
    cleaned_up = 0

    class _FakeProgressActor:
        class _DrainStatus:
            @staticmethod
            def remote() -> dict[str, Any]:
                nonlocal drain_calls
                drain_calls += 1
                return {
                    "total_nodes": 2,
                    "completed_nodes": 1,
                    "failed_nodes": 1 if drain_calls >= 2 else 0,
                    "rejected": False,
                    "truncated": False,
                }

        class _Snapshot:
            @staticmethod
            def remote() -> dict[str, Any]:
                nonlocal snapshot_calls
                snapshot_calls += 1
                return {
                    "state": "FAILED",
                    "total_nodes": 2,
                    "completed_nodes": 1,
                    "failed_nodes": 1,
                    "graph": {
                        "nodes": [
                            {"node_id": "0.0", "state": "SUCCEEDED", "dependencies": []},
                            {"node_id": "0.1", "state": "FAILED", "dependencies": ["0.0"]},
                        ],
                        "edges": [{"source": "0.0", "target": "0.1"}],
                    },
                    "ingress": {"rejected": False, "truncated": False},
                }

        drain_status = _DrainStatus()
        snapshot = _Snapshot()

    exec_obj = workflows_module._RayExecutor.__new__(workflows_module._RayExecutor)
    exec_obj.reporting_policy = "full"
    exec_obj._terminal_progress_finished = False
    exec_obj._progress_admission_exhausted = False
    exec_obj.progress_actor = _FakeProgressActor()
    exec_obj.workflow_run_identity = WorkflowRunIdentity(1, 1, 0, "run-drain-1")
    exec_obj._pending_leaf_outcomes = {"stuck-sibling-ref": "0.2"}
    exec_obj._poll_leaf_outcomes = lambda: None  # type: ignore[method-assign]
    exec_obj.ray = SimpleNamespace(
        wait=lambda refs, timeout=0: (refs, []),
        get=lambda ref: ref,
        kill=lambda *_a, **_k: None,
    )
    published_snapshots: list[dict[str, Any]] = []

    def _fake_publish(snap: dict[str, Any]) -> bool:
        published_snapshots.append(snap)
        return False  # Exercise published=False cleanup path too!

    exec_obj._publish_terminal_progress = _fake_publish  # type: ignore[method-assign]
    exec_obj._publish_unavailable_terminal_progress = lambda **_kw: None  # type: ignore[method-assign]

    def _spy_cleanup() -> None:
        nonlocal cleaned_up
        cleaned_up += 1
        exec_obj.progress_actor = None

    exec_obj._cleanup_progress_actor = _spy_cleanup  # type: ignore[method-assign]

    exec_obj.finish_progress(failed=True)
    assert drain_calls >= 2
    assert snapshot_calls == 1
    assert len(published_snapshots) == 1
    assert cleaned_up == 1


@pytest.mark.django_db
def test_upsert_topology_pages_batched_verifies_existing_pages_without_n_plus_one() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="batched-restage-task",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
    )
    run_storage = WorkflowProgressRunStorage.objects.create(
        execution=execution,
        attempt_number=1,
        execution_generation=0,
        run_id=uuid.uuid4(),
    )
    pages = tuple(
        PreparedWorkflowProgressTopologyPage(
            collection=WorkflowProgressTopologyCollection.NODE,
            page_index=idx,
            digest=hashlib.sha256(f"page-{idx}".encode()).hexdigest(),
            payload=f"page-{idx}".encode(),
            item_count=1,
            encoded_bytes=len(f"page-{idx}".encode()),
            decoded_bytes=len(f"page-{idx}".encode()),
        )
        for idx in range(12)
    )
    # Initial insert
    _upsert_topology_pages_batched(run_storage=run_storage, pages=pages, using="default")

    # Re-stage of all 12 already-existing pages should verify in 1 batched SELECT (not 12+1).
    with CaptureQueriesContext(connection) as captured:
        restaged = _upsert_topology_pages_batched(
            run_storage=run_storage,
            pages=pages,
            using="default",
        )
    assert len(restaged) == 12
    assert len(captured) == 1


def _list_overwrite_index0_large(acc: list[Any], item: int) -> list[Any]:
    acc[0] = "x" * 100_000
    if item > 1:
        acc.append(item)
    return acc


def _dict_overwrite_k0_large(acc: dict[str, Any], item: int) -> dict[str, Any]:
    acc["k0"] = "x" * 100_000
    acc[f"new_{item}"] = item
    return acc


def _list_inject_mutable_then_fail(acc: list[Any], item: int) -> list[Any]:
    if item == 0:
        acc[0] = [1]
        return acc
    acc[0].append(999)
    raise RuntimeError("nested-list-boom")


def test_result_fold_overwritten_elements_enforce_size_and_rollback() -> None:
    """Codex P2: large mutable list/dict element overwrites must not bypass size accounting."""
    # 1. Same-length overwrite on large list (> _FAST_BOUND_MAX_NODES=32) raises immediately.
    fold_same = WorkflowMapResultFold(
        4,
        4,
        1024,
        f"{__name__}._list_overwrite_index0_large",
        False,
        (),
        {},
        list(range(40)),
    )
    with pytest.raises(ResultFoldOverflowError, match="accumulator serialization"):
        fold_same.append(0, 1)
    assert fold_same._accumulator == list(range(40))

    # 2. Overwrite + append on large list also raises immediately and rolls back cleanly.
    fold_grow = WorkflowMapResultFold(
        4,
        4,
        1024,
        f"{__name__}._list_overwrite_index0_large",
        False,
        (),
        {},
        list(range(40)),
    )
    with pytest.raises(ResultFoldOverflowError, match="accumulator serialization"):
        fold_grow.append(0, 2)
    assert fold_grow._accumulator == list(range(40))

    # 3. Overwrite + new key on large dict (>32 entries) raises immediately and rolls back.
    initial_dict = {f"k{i}": i for i in range(40)}
    fold_dict = WorkflowMapResultFold(
        4,
        4,
        2048,
        f"{__name__}._dict_overwrite_k0_large",
        False,
        (),
        {},
        dict(initial_dict),
    )
    with pytest.raises(ResultFoldOverflowError, match="accumulator serialization"):
        fold_dict.append(0, 1)
    assert fold_dict._accumulator == initial_dict

    # 4. Overwriting an immutable list element with a nested mutable list preserves deep rollback
    #    even when the accumulator already has >32 elements.
    fold_nested = WorkflowMapResultFold(
        4,
        4,
        16384,
        f"{__name__}._list_inject_mutable_then_fail",
        False,
        (),
        {},
        list(range(50)),
    )
    fold_nested.append(0, 0)
    assert fold_nested._accumulator == [[1], *range(1, 50)]
    with pytest.raises(RuntimeError, match="nested-list-boom"):
        fold_nested.append(1, 1)
    assert fold_nested._accumulator == [[1], *range(1, 50)]

    # 5. Overwriting index 0 on a list exceeding _FAST_BOUND_MAX_NODES still enforces exact limit.
    large_initial = list(range(2100))
    fold_huge = WorkflowMapResultFold(
        4,
        4,
        32 * 1024,
        f"{__name__}._list_overwrite_index0_large",
        False,
        (),
        {},
        list(large_initial),
    )
    with pytest.raises(ResultFoldOverflowError, match="accumulator serialization"):
        fold_huge.append(0, 1)
    assert fold_huge._accumulator == large_initial


def test_dynamic_strategy_admit_uses_task_pk_and_respects_read_only_replay() -> None:
    """DynamicTasksWorkflowExecutionStrategy.admit reads DurableTaskContext.task_pk safely."""
    from django_ray.runtime.context import durable_task_execution
    from django_ray.workflow.plans import materialize_workflow_plan
    from django_ray.workflow.strategy import DynamicTasksWorkflowExecutionStrategy

    sig = workflows_module.step(_sum_reducer)
    plan = materialize_workflow_plan(sig)
    strategy = DynamicTasksWorkflowExecutionStrategy()
    selection = strategy.validate(
        plan, requested_policy="dynamic_tasks", reporting_policy="disabled"
    )
    prepared = strategy.prepare(plan, selection=selection)

    with durable_task_execution(42, task_id="dyn-42", attempt_number=2, execution_generation=3):
        admitted = strategy.admit(prepared)
        assert admitted.invocation_identity.run_identity.task_execution_pk == 42
        assert admitted.invocation_identity.run_identity.attempt_number == 2
        assert admitted.invocation_identity.run_identity.execution_generation == 3

    with durable_task_execution(
        42,
        task_id="dyn-42",
        attempt_number=2,
        execution_generation=3,
        read_only_replay=True,
    ):
        replayed = strategy.admit(prepared)
        assert replayed.invocation_identity.run_identity.run_id == "local-workflow-run"


_NON_CALLABLE_SENTINEL = 12345


def test_import_utils_validation_and_unconfigured_settings_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover runtime/import_utils.py validation, unconfigured settings, and fallback branches."""
    from django.apps import apps
    from django.conf import settings as django_settings

    from django_ray.runtime import import_utils

    assert import_utils._is_valid_dotted_module("") is False
    with pytest.raises(ImportError, match="at least one dot"):
        import_utils._split_dotted_path("nodot")
    with pytest.raises(ImportError, match="valid dotted Python identifier"):
        import_utils._split_dotted_path("bad-mod.fn")
    with pytest.raises(ValueError, match="sequence of dotted"):
        import_utils._normalize_allowed_modules("not_a_list")
    with pytest.raises(ValueError, match="valid dotted Python module prefixes"):
        import_utils._normalize_allowed_modules(["valid.mod", "bad!mod"])

    # _default_workflow_allowed_modules when apps.ready is False
    monkeypatch.setattr(apps, "ready", False)
    monkeypatch.setattr(
        django_settings,
        "INSTALLED_APPS",
        [
            "",
            123,
            "django.contrib.admin",
            "django_ray",
            "bad!app",
            "myapp.apps.MyAppConfig",
            "myapp",
        ],
        raising=False,
    )
    derived = import_utils._default_workflow_allowed_modules()
    assert "myapp" in derived
    assert "django" not in derived
    assert "django_ray" not in derived
    assert "bad!app" not in derived
    assert derived.count("myapp") == 1
    monkeypatch.undo()

    # Unconfigured settings branch in _configured_allowed_callable_modules,
    # _default_workflow_allowed_modules, and get_driver_allowed_callable_modules
    fake_conf = SimpleNamespace(settings=SimpleNamespace(configured=False))
    monkeypatch.setitem(sys.modules, "django.conf", fake_conf)
    assert import_utils._configured_allowed_callable_modules() is None
    assert import_utils._default_workflow_allowed_modules() == ()
    assert import_utils.get_driver_allowed_callable_modules() == ()
    with import_utils.active_allowed_callable_modules(["custom.pkg"]):
        assert import_utils.get_driver_allowed_callable_modules() == ("custom.pkg",)
    monkeypatch.undo()

    # Non-mapping DJANGO_RAY returns None
    monkeypatch.setattr(django_settings, "DJANGO_RAY", "not-a-dict", raising=False)
    assert import_utils._configured_allowed_callable_modules() is None
    monkeypatch.undo()

    # resolve_task_callable, resolve_workflow_callable, and import_callable error paths
    with pytest.raises(ImportError, match="not permitted"):
        import_utils.resolve_task_callable("forbidden_pkg.mod.fn", allowed_modules=["allowed_pkg"])
    with pytest.raises(TypeError, match="not a Django Task"):
        import_utils.resolve_task_callable(
            f"{__name__}._NON_CALLABLE_SENTINEL",
            allowed_modules=[__name__],
        )
    with pytest.raises(ImportError, match="not permitted"):
        import_utils.resolve_workflow_callable(
            "forbidden_pkg.mod.fn", allowed_modules=["allowed_pkg"]
        )
    with pytest.raises(TypeError, match="not callable"):
        import_utils.resolve_workflow_callable(
            f"{__name__}._NON_CALLABLE_SENTINEL",
            allowed_modules=[__name__],
        )
    with pytest.raises(ImportError, match="at least one dot"):
        import_utils.import_callable("nodot")
    with pytest.raises(ImportError, match="Could not import module"):
        import_utils.import_callable("nonexistent_module_xyz.fn")
    with pytest.raises(AttributeError, match="has no attribute"):
        import_utils.import_callable(f"{__name__}.no_such_attr_xyz")
    with pytest.raises(TypeError, match="not callable"):
        import_utils.import_callable(f"{__name__}._NON_CALLABLE_SENTINEL")


def test_result_fold_protocol_bounds_and_helper_edge_branches() -> None:
    """Cover remaining pure-Python edge branches in runtime/result_fold.py."""
    from django_ray.execution_codec import NestedExecutionRequestRejected
    from django_ray.runtime import result_fold as rf

    # _inspect_serialized_upper_bound: primitives, nested mutables, cycles, >max_nodes, custom
    bound, immutable = rf._inspect_serialized_upper_bound((1.5, b"abc", frozenset({1})))
    assert bound is not None and immutable is True
    _, nested_list_immut = rf._inspect_serialized_upper_bound([1, [2]])
    assert nested_list_immut is False
    _, nested_dict_immut = rf._inspect_serialized_upper_bound({"a": {"b": 1}})
    assert nested_dict_immut is False
    _, bytearray_immut = rf._inspect_serialized_upper_bound([bytearray(b"ab")])
    assert bytearray_immut is False
    assert rf._inspect_serialized_upper_bound("x" * 100, max_bytes=32) == (None, False)
    assert rf._inspect_serialized_upper_bound([1], max_bytes=8) == (None, False)
    cyclic_list: list[Any] = []
    cyclic_list.append(cyclic_list)
    assert rf._estimate_serialized_upper_bound(cyclic_list) is None
    cyclic_dict: dict[str, Any] = {}
    cyclic_dict["self"] = cyclic_dict
    assert rf._estimate_serialized_upper_bound(cyclic_dict) is None
    assert rf._estimate_serialized_upper_bound(list(range(20)), max_nodes=10) is None
    assert rf._estimate_serialized_upper_bound({i: i for i in range(20)}, max_nodes=10) is None
    assert rf._estimate_serialized_upper_bound(object()) is None

    # allowed_callable_modules validation & partial nested expectations
    with pytest.raises(TypeError, match="sequence of strings"):
        WorkflowMapResultFold(
            4, 4, 4096, f"{__name__}._sum_reducer", False, (), {}, 0, allowed_callable_modules="bad"
        )
    actor_bad_nested = WorkflowMapResultFold(
        4,
        4,
        4096,
        f"{__name__}._sum_reducer",
        False,
        (),
        {},
        0,
        nested_execution_request=None,
        expected_outer_task_execution_pk=1,
    )
    with pytest.raises(NestedExecutionRequestRejected):
        actor_bad_nested.ready()

    actor_missing_exp = WorkflowMapResultFold(
        4,
        4,
        4096,
        f"{__name__}._sum_reducer",
        False,
        (),
        {},
        0,
        nested_execution_request="token-without-expectations",
    )
    with pytest.raises(NestedExecutionRequestRejected):
        actor_missing_exp.ready()

    # Out-of-order oversized item & append_batch validation errors
    fold = WorkflowMapResultFold(4, 4, 128, f"{__name__}._sum_reducer", False, (), {}, 0)
    with pytest.raises(ResultFoldOverflowError, match="item serialization at index 1"):
        fold.append(1, "z" * 512)
    with pytest.raises(rf.ResultFoldProtocolError, match="sequence of pairs"):
        fold.append_batch("not-a-seq")  # type: ignore[arg-type]
    with pytest.raises(rf.ResultFoldProtocolError, match="tuples"):
        fold.append_batch([(0,)])  # type: ignore[list-item]
    fold.append(0, 1)
    fold.finalize(1)
    with pytest.raises(rf.ResultFoldProtocolError, match="after result-fold finalization"):
        fold.append_batch([(1, 2)])
