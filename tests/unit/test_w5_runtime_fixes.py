"""Wave 5A surgical regression tests for R3-001, R3-004, R3-005, R3-006, and R3-019."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import django.conf
import pytest

from django_ray import workflows as workflows_module
from django_ray.execution_codec import (
    NestedExecutionRequestRejected,
    NestedExecutionRequestRejection,
)
from django_ray.runtime import import_utils
from django_ray.runtime.context import durable_task_execution
from django_ray.runtime.remote import execute_workflow_step_remote
from django_ray.runtime.result_buffer import ResultBufferProtocolError, WorkflowMapResultBuffer
from django_ray.runtime.result_fold import ResultFoldProtocolError, WorkflowMapResultFold
from django_ray.runtime.runtime_env import ResolvedRuntimeEnv
from django_ray.workflow.dsl import _DslMapNode
from django_ray.workflow.plans import WorkflowPlanMismatchError, runtime_env_plan_identity
from django_ray.workflows import (
    WorkflowExpr,
    branch,
    chain,
    map_step,
    parallel,
    step,
)

# ---------------------------------------------------------------------------
# Module-level workflow helper callables (importable & allowlist-safe)
# ---------------------------------------------------------------------------


def _double(x: int) -> int:
    return x * 2


def _add_one(x: int) -> int:
    return x + 1


def _sum_reducer(acc: int, item: int) -> int:
    return acc + item


def _is_positive(x: int) -> bool:
    return x > 0


def _negate(x: int) -> int:
    return -x


def _always_fail_value_error(x: int) -> int:
    raise ValueError(f"boom:{x}")


def _fallback_recover(x: int) -> int:
    return x + 100


def _raise_nested_rejection(x: int) -> int:
    del x
    raise NestedExecutionRequestRejected(NestedExecutionRequestRejection.MISSING_CONTEXT)


def _raise_plan_mismatch(x: int) -> int:
    del x
    raise WorkflowPlanMismatchError("stale plan")


def _nested_non_django_workflow_step(x: int) -> dict[str, Any]:
    """Run a nested workflow while Django settings are unconfigured."""
    inherited = import_utils.get_driver_allowed_callable_modules()
    wf_result = chain(step(_double), step(_add_one)).run(x, use_ray=False)
    return {"inherited": inherited, "result": wf_result}


def _reducer_capturing_allowlist(acc: list[Any], item: int) -> list[Any]:
    return [*acc, (item, import_utils.get_driver_allowed_callable_modules())]


# ---------------------------------------------------------------------------
# Helpers simulating Ray Core & Ray Client ObjectRef semantics strictly
# ---------------------------------------------------------------------------


class ObjectRef:
    """Strict Ray ObjectRef stand-in that fails if passed nested inside containers."""

    def __init__(self, object_id: str, value: Any) -> None:
        self.object_id = object_id
        self._value = value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ObjectRef) and self.object_id == other.object_id

    def __hash__(self) -> int:
        return hash(self.object_id)

    def __repr__(self) -> str:
        return f"ObjectRef({self.object_id!r})"


class ClientObjectRef:
    """Ray Client ObjectRef stand-in where ray.wait() returns fresh wrapper instances."""

    def __init__(self, object_id: str, value: Any) -> None:
        self.object_id = object_id
        self._value = value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ClientObjectRef) and self.object_id == other.object_id

    def __hash__(self) -> int:
        return hash(self.object_id)

    def __repr__(self) -> str:
        return f"ClientObjectRef({self.object_id!r})"


def _assert_no_nested_object_refs(value: Any) -> None:
    """Fail immediately if an ObjectRef/ClientObjectRef is nested inside a container."""
    if isinstance(value, (ObjectRef, ClientObjectRef)):
        raise AssertionError(f"Nested ObjectRef passed to Ray .remote(): {value!r}")
    if isinstance(value, (list, tuple, set, frozenset)):
        for entry in value:
            _assert_no_nested_object_refs(entry)
    elif isinstance(value, dict):
        for k, v in value.items():
            _assert_no_nested_object_refs(k)
            _assert_no_nested_object_refs(v)


def _unbox_top_level_ray_arg(arg: Any) -> Any:
    """Mirror Ray semantics: auto-dereference top-level ObjectRefs, NEVER nested ones."""
    if isinstance(arg, (ObjectRef, ClientObjectRef)):
        return arg._value
    _assert_no_nested_object_refs(arg)
    return arg


class _StrictRayRemoteMethod:
    """Simulates a Ray actor method enforcing top-level-only ObjectRef unboxing."""

    def __init__(self, fn: Any, call_log: list[str], name: str) -> None:
        self._fn = fn
        self._call_log = call_log
        self._name = name

    def options(self, **_kwargs: Any) -> _StrictRayRemoteMethod:
        return self

    def remote(self, *args: Any, **kwargs: Any) -> Any:
        self._call_log.append(self._name)
        unboxed_args = [_unbox_top_level_ray_arg(arg) for arg in args]
        unboxed_kwargs = {key: _unbox_top_level_ray_arg(val) for key, val in kwargs.items()}
        return self._fn(*unboxed_args, **unboxed_kwargs)


# ---------------------------------------------------------------------------
# R3-001 [P0]: Real-Ray batch ObjectRef unboxing & value-based ready-ref match
# ---------------------------------------------------------------------------


def test_result_buffer_and_fold_append_batch_values_protocol(settings: Any) -> None:
    settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}

    buf = WorkflowMapResultBuffer(max_items=4, max_serialized_bytes=16384)
    acks = buf.append_batch_values([0, 1, 2], "a", "b", "c")
    assert [ack["index"] for ack in acks] == [0, 1, 2]
    assert buf.finalize(3)[0] == ["a", "b", "c"]

    with pytest.raises(ResultBufferProtocolError, match="indices must be a sequence"):
        buf_err = WorkflowMapResultBuffer(max_items=4, max_serialized_bytes=16384)
        buf_err.append_batch_values("01", "a", "b")  # type: ignore[arg-type]

    with pytest.raises(ResultBufferProtocolError, match="counts do not match"):
        buf_err2 = WorkflowMapResultBuffer(max_items=4, max_serialized_bytes=16384)
        buf_err2.append_batch_values([0, 1], "only-one")

    fold = WorkflowMapResultFold(4, 4, 16384, f"{__name__}._sum_reducer", False, (), {}, 0)
    fold_acks = fold.append_batch_values([0, 1, 2], 10, 20, 30)
    assert [ack["index"] for ack in fold_acks] == [0, 1, 2]
    assert fold.finalize(3)[0] == 60

    with pytest.raises(ResultFoldProtocolError, match="indices must be a sequence"):
        fold_err = WorkflowMapResultFold(4, 4, 16384, f"{__name__}._sum_reducer", False, (), {}, 0)
        fold_err.append_batch_values("01", 1, 2)  # type: ignore[arg-type]

    with pytest.raises(ResultFoldProtocolError, match="counts do not match"):
        fold_err2 = WorkflowMapResultFold(4, 4, 16384, f"{__name__}._sum_reducer", False, (), {}, 0)
        fold_err2.append_batch_values([0, 1], 10)


def test_workflows_map_batch_never_passes_nested_object_refs(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    """Prove append_result_{buffer,fold}_batch unpacks ObjectRefs as top-level args."""
    settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
    buf_actor = WorkflowMapResultBuffer(max_items=8, max_serialized_bytes=32768)
    fold_actor = WorkflowMapResultFold(8, 8, 32768, f"{__name__}._sum_reducer", False, (), {}, 0)
    buf_calls: list[str] = []
    fold_calls: list[str] = []

    class _StrictActorProxy:
        def __init__(self, target: Any, call_log: list[str]) -> None:
            self.ready = _StrictRayRemoteMethod(target.ready, call_log, "ready")
            self.append = _StrictRayRemoteMethod(target.append, call_log, "append")
            self.append_batch = _StrictRayRemoteMethod(
                target.append_batch, call_log, "append_batch"
            )
            self.append_batch_values = _StrictRayRemoteMethod(
                target.append_batch_values, call_log, "append_batch_values"
            )
            self.finalize = _StrictRayRemoteMethod(target.finalize, call_log, "finalize")
            self.discard = _StrictRayRemoteMethod(target.discard, call_log, "discard")

    class _StrictFakeRay:
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
            return ref._value if isinstance(ref, ObjectRef) else ref

        @staticmethod
        def kill(*_args: Any, **_kwargs: Any) -> None:
            return None

    class _StrictRayExec(workflows_module._RayExecutor):
        def __init__(self) -> None:
            self.ray = _StrictFakeRay()
            self.task_context = None
            self.materialized_plan = None
            self.progress_actor = None
            self.workflow_run_identity = None
            self.reporting_policy = "disabled"
            self._progress_suppression_depth = 0
            self._pending_leaf_outcomes = {}
            self._counter = 0

        def submit_step(
            self,
            signature: Any,
            input_args: tuple[Any, ...],
            input_kwargs: dict[str, Any],
            node_id: str,
            dependencies: tuple[str, ...],
        ) -> ObjectRef:
            del node_id, dependencies
            self._counter += 1
            fn = import_utils.resolve_workflow_callable(signature.callable_path)
            return ObjectRef(f"obj-{self._counter}", fn(*input_args, **input_kwargs))

        def resolve(self, value: Any) -> Any:
            return value._value if isinstance(value, ObjectRef) else value

        def start_result_buffer(self, **_kwargs: Any) -> Any:
            return workflows_module._RayResultBufferSession(
                actor=_StrictActorProxy(buf_actor, buf_calls)
            )

        def start_result_fold(self, **_kwargs: Any) -> Any:
            return workflows_module._RayResultFoldSession(
                actor=_StrictActorProxy(fold_actor, fold_calls),
                maximum_items=8,
                maximum_concurrency=8,
                maximum_out_of_order_items=7,
                maximum_serialized_bytes=32768,
            )

    monkeypatch.setattr(workflows_module, "_get_executor", lambda _u, **_kw: _StrictRayExec())

    buf_wf = (
        map_step(step(_double))
        .with_limits(max_concurrency=6, max_items=8)
        .with_result_buffer(
            max_serialized_bytes=32768,
            actor_options={"num_cpus": 0.25, "memory": 32768},
        )
    )
    assert buf_wf.run([1, 2, 3, 4, 5, 6], use_ray=True) == [2, 4, 6, 8, 10, 12]
    assert "append_batch_values" in buf_calls
    assert "append_batch" not in buf_calls

    fold_wf = (
        map_step(step(_double))
        .with_limits(max_concurrency=6, max_items=8)
        .reduce(
            step(_sum_reducer),
            initial=0,
            max_serialized_bytes=32768,
            actor_options={"num_cpus": 0.25, "memory": 32768},
        )
    )
    assert fold_wf.run([1, 2, 3, 4, 5, 6], use_ray=True) == 42
    assert "append_batch_values" in fold_calls
    assert "append_batch" not in fold_calls


def test_workflows_map_batch_fallback_avoids_nested_object_refs_without_variadic_actor(
    settings: Any,
) -> None:
    """If an actor proxy lacks append_batch_values and items carry ObjectRefs, use append()."""
    settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
    buf_actor = WorkflowMapResultBuffer(max_items=4, max_serialized_bytes=16384)
    fold_actor = WorkflowMapResultFold(4, 4, 16384, f"{__name__}._sum_reducer", False, (), {}, 0)
    buf_calls: list[str] = []
    fold_calls: list[str] = []

    class _LegacyActorProxy:
        def __init__(self, target: Any, call_log: list[str]) -> None:
            self.append = _StrictRayRemoteMethod(target.append, call_log, "append")
            self.append_batch = _StrictRayRemoteMethod(
                target.append_batch, call_log, "append_batch"
            )

    executor = object.__new__(workflows_module._RayExecutor)
    executor.progress_actor = None
    executor.ray = type("_Ray", (), {"get": staticmethod(lambda v: v)})()

    buf_session = workflows_module._RayResultBufferSession(
        actor=_LegacyActorProxy(buf_actor, buf_calls)
    )
    executor.append_result_buffer_batch(
        buf_session,
        items=[(0, ObjectRef("r0", 10)), (1, ObjectRef("r1", 20))],
    )
    assert buf_calls == ["append", "append"]
    assert buf_actor.finalize(2)[0] == [10, 20]

    fold_session = workflows_module._RayResultFoldSession(
        actor=_LegacyActorProxy(fold_actor, fold_calls),
        maximum_items=4,
        maximum_concurrency=4,
        maximum_out_of_order_items=3,
        maximum_serialized_bytes=16384,
    )
    folded = executor.append_result_fold_batch(
        fold_session,
        items=[(0, ObjectRef("f0", 5)), (1, ObjectRef("f1", 7))],
    )
    assert folded == 2
    assert fold_calls == ["append", "append"]
    assert fold_actor.finalize(2)[0] == 12


def test_wait_ready_leaf_indices_matches_distinct_ray_client_wrappers(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    """Distinct ClientObjectRef instances returned by ray.wait() must match by value, not id()."""
    settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
    buf_actor = WorkflowMapResultBuffer(max_items=6, max_serialized_bytes=16384)
    buf_calls: list[str] = []

    class _ClientFakeRay:
        @staticmethod
        def wait(
            refs: list[Any],
            *,
            num_returns: int = 1,
            timeout: float | None = None,
            fetch_local: bool = True,
        ) -> tuple[list[Any], list[Any]]:
            del timeout, fetch_local
            ready = [
                ClientObjectRef(r.object_id, r._value) if isinstance(r, ClientObjectRef) else r
                for r in refs[:num_returns]
            ]
            # Verify distinct Python object identities (Ray Client behavior)
            for orig, cloned in zip(refs[:num_returns], ready, strict=True):
                if isinstance(orig, ClientObjectRef):
                    assert orig is not cloned
                    assert orig == cloned
            return ready, refs[num_returns:]

        @staticmethod
        def get(ref: Any) -> Any:
            return ref._value if isinstance(ref, ClientObjectRef) else ref

        @staticmethod
        def kill(*_args: Any, **_kwargs: Any) -> None:
            return None

    class _StrictClientActorProxy:
        def __init__(self, target: Any) -> None:
            self.ready = _StrictRayRemoteMethod(target.ready, buf_calls, "ready")
            self.append = _StrictRayRemoteMethod(target.append, buf_calls, "append")
            self.append_batch_values = _StrictRayRemoteMethod(
                target.append_batch_values, buf_calls, "append_batch_values"
            )
            self.finalize = _StrictRayRemoteMethod(target.finalize, buf_calls, "finalize")
            self.discard = _StrictRayRemoteMethod(target.discard, buf_calls, "discard")

    class _ClientRayExec(workflows_module._RayExecutor):
        def __init__(self) -> None:
            self.ray = _ClientFakeRay()
            self.task_context = None
            self.materialized_plan = None
            self.progress_actor = None
            self.workflow_run_identity = None
            self.reporting_policy = "disabled"
            self._progress_suppression_depth = 0
            self._pending_leaf_outcomes = {}
            self._counter = 0

        def submit_step(
            self,
            signature: Any,
            input_args: tuple[Any, ...],
            input_kwargs: dict[str, Any],
            node_id: str,
            dependencies: tuple[str, ...],
        ) -> ClientObjectRef:
            del node_id, dependencies
            self._counter += 1
            fn = import_utils.resolve_workflow_callable(signature.callable_path)
            return ClientObjectRef(f"client-obj-{self._counter}", fn(*input_args, **input_kwargs))

        def resolve(self, value: Any) -> Any:
            return value._value if isinstance(value, ClientObjectRef) else value

        def start_result_buffer(self, **_kwargs: Any) -> Any:
            return workflows_module._RayResultBufferSession(
                actor=_StrictClientActorProxy(buf_actor)
            )

    # Direct verification of _wait_ready_leaf_indices with distinct wrappers
    exec_direct = _ClientRayExec()
    refs = [ClientObjectRef(f"id-{i}", i) for i in range(4)]
    assert exec_direct.wait_result_buffer_leaves(refs, max_batch=4) == [0, 1, 2, 3]
    # Also verify duplicate/unhashable stub matching works 1-to-1
    assert workflows_module._matching_ref_indices([{"a": 1}, {"a": 1}], [{"a": 1}]) == [0]
    assert workflows_module._matching_ref_indices([7, 7, 8], [7]) == [0]

    monkeypatch.setattr(workflows_module, "_get_executor", lambda _u, **_kw: _ClientRayExec())
    wf = (
        map_step(step(_double))
        .with_limits(max_concurrency=4, max_items=6)
        .with_result_buffer(
            max_serialized_bytes=16384,
            actor_options={"num_cpus": 0.25, "memory": 32768},
        )
    )
    assert wf.run([1, 2, 3, 4], use_ray=True) == [2, 4, 6, 8]
    assert "append_batch_values" in buf_calls


def _build_real_ray_batch_workflows() -> tuple[Any, Any]:
    """Build validated result-buffer and result-fold workflows for real-Ray batching."""
    buf_wf = (
        map_step(step(_double, ray_options={"num_cpus": 0.25}))
        .with_limits(max_concurrency=4, max_items=8)
        .with_result_buffer(
            max_serialized_bytes=65536,
            actor_options={"num_cpus": 0.25, "memory": 1024 * 1024},
        )
    )
    fold_wf = (
        map_step(step(_double, ray_options={"num_cpus": 0.25}))
        .with_limits(max_concurrency=4, max_items=8)
        .reduce(
            step(_sum_reducer),
            initial=0,
            max_serialized_bytes=65536,
            actor_options={"num_cpus": 0.25, "memory": 1024 * 1024},
        )
    )
    return buf_wf, fold_wf


def test_real_ray_batch_workflow_builders_validate_cleanly() -> None:
    """Validate real-Ray batch workflow options eagerly on non-Ray platforms too."""
    buf_wf, fold_wf = _build_real_ray_batch_workflows()
    assert buf_wf.result_buffer is not None
    assert buf_wf.result_buffer.actor_options["num_cpus"] == 0.25
    assert buf_wf.result_buffer.actor_options["memory"] == 1024 * 1024
    assert buf_wf.signature.ray_options == {"num_cpus": 0.25}
    assert fold_wf.result_fold is not None
    assert fold_wf.result_fold.actor_options["num_cpus"] == 0.25
    assert fold_wf.result_fold.actor_options["memory"] == 1024 * 1024
    assert fold_wf.signature.ray_options == {"num_cpus": 0.25}


@pytest.fixture(scope="module")
def ray_cluster():
    from tests.local_ray import local_ray_runtime

    with local_ray_runtime(num_cpus=4) as ray:
        yield ray


@pytest.mark.real_ray
def test_real_ray_map_batch_unboxes_result_buffer_and_result_fold(
    ray_cluster: Any,
    settings: Any,
) -> None:
    """Real-Ray end-to-end proof that multi-leaf batching unboxes ObjectRefs cleanly."""
    del ray_cluster
    settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
    buf_wf, fold_wf = _build_real_ray_batch_workflows()
    assert buf_wf.run([1, 2, 3, 4, 5, 6, 7, 8], use_ray=True) == [2, 4, 6, 8, 10, 12, 14, 16]
    assert fold_wf.run([1, 2, 3, 4, 5, 6, 7, 8], use_ray=True) == 72


# ---------------------------------------------------------------------------
# R3-004 [P0 runtime slice] & R3-019: Allowlist ContextVar & error wording
# ---------------------------------------------------------------------------


def test_active_allowed_callable_modules_fallback_and_precedence(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    """ContextVar fallback applies ONLY when Django settings are unconfigured."""
    # 1. Explicit Django settings ALWAYS take precedence over ContextVar (even empty deny-all).
    settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": []}
    with import_utils.active_allowed_callable_modules([__name__]):
        assert import_utils.get_driver_allowed_callable_modules() == ()

    settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": ["testproject"]}
    with import_utils.active_allowed_callable_modules([__name__]):
        assert import_utils.get_driver_allowed_callable_modules() == ("testproject",)

    # 2. When Django settings are unconfigured: fail-closed if unset, inherit when bound.
    monkeypatch.setattr(django.conf, "settings", SimpleNamespace(configured=False))
    assert import_utils.get_driver_allowed_callable_modules() == ()
    with pytest.raises(ImportError, match="not permitted"):
        import_utils.resolve_workflow_callable(f"{__name__}._double")

    with import_utils.active_allowed_callable_modules([__name__]):
        assert import_utils.get_driver_allowed_callable_modules() == (__name__,)
        assert import_utils.resolve_workflow_callable(f"{__name__}._double") is _double

    # Resets cleanly after exiting context manager.
    assert import_utils.get_driver_allowed_callable_modules() == ()


def test_execute_workflow_step_remote_propagates_allowlist_to_nested_non_django_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-Django remote steps propagate allowed_callable_modules to nested workflows."""
    monkeypatch.setattr(django.conf, "settings", SimpleNamespace(configured=False))
    output = execute_workflow_step_remote(
        f"{__name__}._nested_non_django_workflow_step",
        False,
        (),
        {},
        {},
        None,
        None,
        "0",
        5,
        allowed_callable_modules=(__name__,),
    )
    assert output == {"inherited": (__name__,), "result": 11}
    # Verify ContextVar did not leak out of the step invocation.
    assert import_utils.get_driver_allowed_callable_modules() == ()


def test_result_fold_execution_context_propagates_allowlist_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WorkflowMapResultFold propagates allowed_callable_modules inside its execution context."""
    monkeypatch.setattr(django.conf, "settings", SimpleNamespace(configured=False))
    fold = WorkflowMapResultFold(
        2,
        2,
        16384,
        f"{__name__}._reducer_capturing_allowlist",
        False,
        (),
        {},
        [],
        allowed_callable_modules=(__name__,),
    )
    fold.append(0, 7)
    assert fold.finalize(1)[0] == [(7, (__name__,))]
    assert import_utils.get_driver_allowed_callable_modules() == ()


def test_import_utils_error_messages_reference_settings_and_installed_apps(settings: Any) -> None:
    """R3-019: Rejection messages accurately name ALLOWED_CALLABLE_MODULES and INSTALLED_APPS."""
    settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": ["testproject"]}

    with pytest.raises(ImportError) as task_exc:
        import_utils.resolve_task_callable(f"{__name__}._double")
    msg = str(task_exc.value)
    assert "not permitted" in msg
    assert "DJANGO_RAY['ALLOWED_CALLABLE_MODULES']" in msg
    assert "INSTALLED_APPS" in msg

    with pytest.raises(ImportError) as wf_exc:
        import_utils.resolve_workflow_callable(f"{__name__}._double")
    wf_msg = str(wf_exc.value)
    assert "not permitted" in wf_msg
    assert "DJANGO_RAY['ALLOWED_CALLABLE_MODULES']" in wf_msg
    assert "INSTALLED_APPS" in wf_msg


# ---------------------------------------------------------------------------
# R3-005 [P1]: Durable DSL branch() / .with_fallback() binding & error safety
# ---------------------------------------------------------------------------


def _make_strict_runtime_env_identity() -> dict[str, Any]:
    resolved = ResolvedRuntimeEnv(
        profile=None,
        spec={},
        serialized="{}",
        digest="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    )
    return runtime_env_plan_identity(resolved).as_transport_dict()


@pytest.mark.django_db
def test_durable_dsl_branch_and_fallback_bind_dynamic_run_and_finish_progress(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    """Conditional/fallback WorkflowExpr.run() inside durable_task_execution binds a fenced run."""
    from django_ray.models import RayTaskExecution, TaskState

    settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
    execution = RayTaskExecution.objects.create(
        task_id="w5-dsl-durable-1",
        callable_path="testproject.tasks.add",
        queue_name="default",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=0,
    )
    captured_requests: list[dict[str, Any]] = []
    finish_calls: list[bool] = []

    class _FakeRemoteStep:
        def options(self, **_opts: Any) -> _FakeRemoteStep:
            return self

        def remote(
            self,
            callable_path: str,
            _bootstrap_django: bool,
            bound_args: tuple[Any, ...],
            bound_kwargs: dict[str, Any],
            input_kwargs: dict[str, Any],
            _task_pk: int | None,
            _progress_actor: Any,
            node_id: str,
            *input_args: Any,
            **remote_kwargs: Any,
        ) -> Any:
            captured_requests.append({"node_id": node_id, **remote_kwargs})
            fn = import_utils.resolve_workflow_callable(callable_path)
            return fn(*input_args, *bound_args, **{**input_kwargs, **bound_kwargs})

    class _FakeRay:
        @staticmethod
        def is_initialized() -> bool:
            return True

        @staticmethod
        def get(val: Any) -> Any:
            return val

        @staticmethod
        def wait(refs: list[Any], **_kw: Any) -> tuple[list[Any], list[Any]]:
            return refs[:1], refs[1:]

        @staticmethod
        def cancel(*_a: Any, **_kw: Any) -> None:
            return None

    def _build_durable_ray_executor() -> workflows_module._RayExecutor:
        ex = object.__new__(workflows_module._RayExecutor)
        ex.ray = _FakeRay()
        ex.materialized_plan = None
        from django_ray.runtime.context import get_current_task_context
        from django_ray.workflow.progress.limits import WORKFLOW_PROGRESS_LIMITS_V1

        ex.task_context = get_current_task_context()
        ex.task_execution_pk = ex.task_context.task_pk if ex.task_context is not None else None
        ex.remote_step = _FakeRemoteStep()
        ex.remote_collect = _FakeRemoteStep()
        ex.progress_actor = None
        ex.workflow_run_identity = None
        ex.reporting_policy = "full"
        ex.workflow_progress_limits = WORKFLOW_PROGRESS_LIMITS_V1
        ex._progress_suppression_depth = 0
        ex._pending_leaf_outcomes = {}
        ex._progress_started_at = time.time()
        orig_finish = ex.finish_progress

        def _tracked_finish(*, failed: bool = False) -> None:
            finish_calls.append(failed)
            orig_finish(failed=failed)

        ex.finish_progress = _tracked_finish  # type: ignore[method-assign]
        return ex

    monkeypatch.setattr(workflows_module, "_get_executor", lambda _u: _build_durable_ray_executor())

    plan_identity = _make_strict_runtime_env_identity()
    with durable_task_execution(
        execution.pk,
        task_id=execution.task_id,
        execution_protocol_version=1,
        attempt_number=1,
        execution_generation=0,
        runtime_env_profile=None,
        runtime_env_hash="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
        runtime_env_plan_identity=plan_identity,
        strict_execution_request=True,
    ):
        expr = branch(_is_positive, on_true=step(_double), on_false=step(_negate)) | step(_add_one)
        assert expr.run(5, use_ray=True) == 11

        fb_expr = WorkflowExpr(_always_fail_value_error).with_fallback(
            _fallback_recover, exceptions=(ValueError,)
        )
        assert fb_expr.run(7, use_ray=True) == 107

    execution.refresh_from_db()
    assert execution.workflow_run_id is not None
    assert len(captured_requests) == 4  # 2 branch chain steps + primary + fallback
    for req in captured_requests:
        assert "nested_execution_request" in req
        assert req["expected_outer_task_execution_pk"] == execution.pk
        assert req["expected_workflow_run_id"]
    assert finish_calls == [False, False]


@pytest.mark.django_db
def test_durable_dsl_stale_attempt_raises_plan_mismatch_not_swallowed_by_fallback(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    """A stale durable attempt must raise WorkflowPlanMismatchError and never trigger fallback."""
    from django_ray.models import RayTaskExecution, TaskState

    settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
    execution = RayTaskExecution.objects.create(
        task_id="w5-dsl-stale-1",
        callable_path="testproject.tasks.add",
        queue_name="default",
        state=TaskState.CANCELLED,
        attempt_number=1,
        execution_generation=0,
    )

    def _build_stale_executor() -> workflows_module._RayExecutor:
        from django_ray.runtime.context import get_current_task_context

        ex = object.__new__(workflows_module._RayExecutor)
        ex.materialized_plan = None
        ex.task_context = get_current_task_context()
        ex.progress_actor = None
        ex.workflow_run_identity = None
        ex.reporting_policy = "full"
        return ex

    monkeypatch.setattr(workflows_module, "_get_executor", lambda _u: _build_stale_executor())

    with durable_task_execution(
        execution.pk,
        task_id=execution.task_id,
        execution_protocol_version=1,
        attempt_number=1,
        execution_generation=0,
        runtime_env_profile=None,
        runtime_env_hash="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
        runtime_env_plan_identity=_make_strict_runtime_env_identity(),
        strict_execution_request=True,
    ):
        expr = WorkflowExpr(_double).with_fallback(_fallback_recover, exceptions=(Exception,))
        with pytest.raises(WorkflowPlanMismatchError):
            expr.run(5, use_ray=True)


def test_fallback_never_swallows_protocol_or_plan_mismatch_errors(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    """_FallbackNode must never swallow NestedExecutionRequestRejected or WorkflowPlanMismatch."""
    settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}

    # Direct local exceptions
    expr_nested = WorkflowExpr(_raise_nested_rejection).with_fallback(
        _fallback_recover, exceptions=(Exception,)
    )
    with pytest.raises(NestedExecutionRequestRejected):
        expr_nested.run(1, use_ray=False)

    expr_plan = WorkflowExpr(_raise_plan_mismatch).with_fallback(
        _fallback_recover, exceptions=(Exception,)
    )
    with pytest.raises(WorkflowPlanMismatchError):
        expr_plan.run(1, use_ray=False)

    # Wrapped inside RayTaskError.cause
    class RayTaskError(Exception):
        def __init__(self, cause: Exception) -> None:
            super().__init__(str(cause))
            self.cause = cause

    class _RayErrorExec(workflows_module._Executor):
        def __init__(self, exc: Exception) -> None:
            self._exc = exc

        def submit_step(self, *_a: Any, **_kw: Any) -> Any:
            return "ref"

        def collect(self, values: list[Any]) -> Any:
            return values

        def resolve(self, value: Any) -> Any:
            del value
            raise RayTaskError(self._exc)

    for control_err in (
        NestedExecutionRequestRejected(NestedExecutionRequestRejection.MISSING_CONTEXT),
        WorkflowPlanMismatchError("plan mismatch"),
    ):
        monkeypatch.setattr(
            workflows_module,
            "_get_executor",
            lambda _u, err=control_err: _RayErrorExec(err),
        )
        expr = WorkflowExpr(_double).with_fallback(_fallback_recover, exceptions=(Exception,))
        with pytest.raises(RayTaskError):
            expr.run(1, use_ray=True)


# ---------------------------------------------------------------------------
# R3-006 [P1]: Thread-safe _Executor cleanup stack & fail-fast sibling cancel
# ---------------------------------------------------------------------------


def test_dsl_concurrent_group_and_map_fallback_thread_safe_cleanup_stack(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    """Concurrent ThreadPoolExecutor threads sharing one _RayExecutor never race on cleanup."""
    settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
    barrier = threading.Barrier(4, timeout=5.0)
    submitted_refs: list[ObjectRef] = []
    submit_lock = threading.Lock()

    class _BarrierRayExec(workflows_module._Executor):
        def submit_step(
            self,
            signature: Any,
            input_args: tuple[Any, ...],
            input_kwargs: dict[str, Any],
            node_id: str,
            dependencies: tuple[str, ...],
        ) -> ObjectRef:
            del dependencies
            # Force all 4 worker threads inside capture_cleanup(...) simultaneously!
            barrier.wait()
            fn = import_utils.resolve_workflow_callable(signature.callable_path)
            ref = ObjectRef(f"ref-{node_id}", fn(*input_args, **input_kwargs))
            with submit_lock:
                submitted_refs.append(ref)
            return ref

        def collect(self, values: list[Any]) -> Any:
            return values

        def resolve(self, value: Any) -> Any:
            result = value._value if isinstance(value, ObjectRef) else value
            self.mark_resolved_cleanup(value)
            return result

    monkeypatch.setattr(workflows_module, "_get_executor", lambda _u: _BarrierRayExec())

    # Parallel group of 4 fallback expressions forces ThreadPoolExecutor path.
    group_expr = parallel(
        WorkflowExpr(_double).with_fallback(_fallback_recover),
        WorkflowExpr(_add_one).with_fallback(_fallback_recover),
        WorkflowExpr(_negate).with_fallback(_fallback_recover),
        WorkflowExpr(_double).with_fallback(_fallback_recover),
    )
    assert group_expr.run(5, use_ray=True) == [10, 6, -5, 10]
    assert len(submitted_refs) == 4

    # Map over fallback body with max_concurrency=4 forces ThreadPoolExecutor path too.
    barrier.reset()
    submitted_refs.clear()
    dsl_map = WorkflowExpr._from_node(
        _DslMapNode(
            body=WorkflowExpr(_double).with_fallback(_fallback_recover).node,
            max_concurrency=4,
            max_items=4,
            cancel_timeout_seconds=0.5,
        )
    )
    assert dsl_map.run([1, 2, 3, 4], use_ray=True) == [2, 4, 6, 8]
    assert len(submitted_refs) == 4


def test_dsl_concurrent_group_and_map_fail_fast_cancels_running_sibling_refs(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    """Fail-fast in DSL ThreadPoolExecutor cancels already-submitted sibling Ray ObjectRefs."""
    settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}

    siblings_entered_resolve = threading.Event()
    release_siblings = threading.Event()
    resolved_count_lock = threading.Lock()
    siblings_waiting = 0
    cancelled_calls: list[tuple[list[Any], float]] = []
    fallback_invoked = threading.Event()

    class _FailFastRayExec(workflows_module._Executor):
        def submit_step(
            self,
            signature: Any,
            input_args: tuple[Any, ...],
            input_kwargs: dict[str, Any],
            node_id: str,
            dependencies: tuple[str, ...],
        ) -> ObjectRef:
            del input_kwargs, dependencies
            if signature.callable_path.endswith("_fallback_recover"):
                fallback_invoked.set()
            return ObjectRef(
                f"ref-{node_id}",
                (signature.callable_path, input_args),
            )

        def collect(self, values: list[Any]) -> Any:
            return values

        def resolve(self, value: Any) -> Any:
            nonlocal siblings_waiting
            callable_path, args = value._value
            if callable_path.endswith("_always_fail_value_error"):
                # Wait until siblings have submitted and entered resolve(), then fail.
                assert siblings_entered_resolve.wait(timeout=3.0)
                raise RuntimeError("primary-uncaught-boom")
            with resolved_count_lock:
                siblings_waiting += 1
                if siblings_waiting >= 2:
                    siblings_entered_resolve.set()
            release_siblings.wait(timeout=3.0)
            self.mark_resolved_cleanup(value)
            return args[0]

        def cancel_and_drain_fold_payloads(
            self,
            values: Any,
            *,
            timeout_seconds: float,
        ) -> None:
            cancelled_list = list(values)
            cancelled_calls.append((cancelled_list, timeout_seconds))
            if any(
                isinstance(ref, ObjectRef)
                and any(tag in ref.object_id for tag in ("g1", "g2", "m1", "m2"))
                for ref in cancelled_list
            ):
                release_siblings.set()

    monkeypatch.setattr(workflows_module, "_get_executor", lambda _u: _FailFastRayExec())

    # 1. Group fail-fast: branch 0 raises uncaught RuntimeError while branches 1 & 2 are running.
    expr = parallel(
        WorkflowExpr(_always_fail_value_error).with_fallback(
            _fallback_recover, exceptions=(KeyError,)
        ),
        WorkflowExpr(_double).with_fallback(_fallback_recover, exceptions=(Exception,)),
        WorkflowExpr(_add_one).with_fallback(_fallback_recover, exceptions=(Exception,)),
    )
    try:
        with pytest.raises(RuntimeError, match="primary-uncaught-boom"):
            expr.run(10, use_ray=True)
    finally:
        release_siblings.set()

    cancelled_ids = {ref.object_id for refs, _ in cancelled_calls for ref in refs}
    assert any("g1" in oid for oid in cancelled_ids)
    assert any("g2" in oid for oid in cancelled_ids)
    assert not fallback_invoked.is_set()

    # 2. Map fail-fast: item 0 raises uncaught RuntimeError while items 1 & 2 are running.
    siblings_entered_resolve.clear()
    release_siblings.clear()
    siblings_waiting = 0
    cancelled_calls.clear()

    class _MapFailFastExec(_FailFastRayExec):
        def resolve(self, value: Any) -> Any:
            nonlocal siblings_waiting
            _callable_path, args = value._value
            if args == (0,):
                assert siblings_entered_resolve.wait(timeout=3.0)
                raise RuntimeError("map-item-0-boom")
            with resolved_count_lock:
                siblings_waiting += 1
                if siblings_waiting >= 2:
                    siblings_entered_resolve.set()
            release_siblings.wait(timeout=3.0)
            self.mark_resolved_cleanup(value)
            return args[0]

    monkeypatch.setattr(workflows_module, "_get_executor", lambda _u: _MapFailFastExec())
    map_expr = WorkflowExpr._from_node(
        _DslMapNode(
            body=WorkflowExpr(_double)
            .with_fallback(_fallback_recover, exceptions=(KeyError,))
            .node,
            max_concurrency=3,
            max_items=3,
            cancel_timeout_seconds=0.75,
        )
    )
    try:
        with pytest.raises(RuntimeError, match="map-item-0-boom"):
            map_expr.run([0, 1, 2], use_ray=True)
    finally:
        release_siblings.set()

    map_cancelled_ids = {ref.object_id for refs, _ in cancelled_calls for ref in refs}
    assert any("m1" in oid for oid in map_cancelled_ids)
    assert any("m2" in oid for oid in map_cancelled_ids)
    assert any(timeout == 0.75 for _, timeout in cancelled_calls)
    assert not fallback_invoked.is_set()


def test_standalone_map_step_run_with_literal_iterable_skips_ray_get_on_input(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    """Standalone map_step(...).run([1, 2, 3], use_ray=True) must not call ray.get on raw lists."""
    settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
    get_calls: list[Any] = []

    class _StrictObjectRefRayExecutor(workflows_module._Executor):
        def submit_step(
            self,
            signature: Any,
            input_args: tuple[Any, ...],
            input_kwargs: dict[str, Any],
            node_id: str,
            dependencies: tuple[str, ...],
        ) -> ObjectRef:
            del input_kwargs, dependencies
            return ObjectRef(f"ref-{node_id}", _double(input_args[0]))

        def collect(self, values: list[Any]) -> ObjectRef:
            return ObjectRef("collect", [v._value for v in values])

        def resolve(self, value: Any) -> Any:
            get_calls.append(value)
            if not isinstance(value, ObjectRef):
                raise TypeError(f"Attempting to call get on non-ObjectRef: {value!r}")
            return value._value

        def resolve_ready(self, value: Any) -> Any:
            return self.resolve(value)

        def store(self, value: Any) -> ObjectRef:
            return ObjectRef("stored", value)

        def wait_one(self, values: Any) -> int:
            return 0

    monkeypatch.setattr(workflows_module, "_get_executor", lambda _u: _StrictObjectRefRayExecutor())
    wf = workflows_module.map_step(workflows_module.step(_double)).with_limits(
        max_concurrency=2, max_items=8
    )
    assert wf.run([1, 2, 3, 4], use_ray=True) == [2, 4, 6, 8]
    assert all(isinstance(call, ObjectRef) for call in get_calls)
    with pytest.raises(workflows_module.WorkflowDefinitionError, match="non-string iterable"):
        wf.run(123, use_ray=True)
    with pytest.raises(workflows_module.WorkflowDefinitionError, match="non-string iterable"):
        wf.run("abc", use_ray=True)


def test_protocol2_remote_executor_unwraps_encoded_completion_result_subclass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Protocol 2 remote execution coerces _EncodedCompletionResult before strict decode."""
    from datetime import UTC, datetime

    from django_ray.runtime import entrypoint as entrypoint_module
    from django_ray.runtime import remote as remote_module
    from django_ray.target import execution_codec as target_codec
    from django_ray.target import probe as target_probe

    claimed_at = datetime(2026, 1, 1, tzinfo=UTC)
    fake_request = SimpleNamespace(
        identity=SimpleNamespace(
            task_execution_pk=7,
            task_id="proto2-subclass-test",
            attempt_number=1,
            execution_generation=1,
        ),
        execution_protocol_version=2,
        callable_path="testproject.tasks.add_numbers",
        serialized_args="[1, 2]",
        serialized_kwargs="{}",
        input_reference=None,
        runtime_env_profile=None,
        runtime_env_hash="",
        runtime_env_plan_identity=None,
        compiled_graph_submission_transport=None,
        target_execution_evidence_id=1,
        target_execution_evidence_digest="a" * 64,
        target_execution_claimed_at=claimed_at,
        target_expectation_digest="b" * 64,
        claim_attestation_digest="c" * 64,
    )
    observed_target = SimpleNamespace(marker="obs")
    encoded_subclass = entrypoint_module._EncodedCompletionResult(
        '{"success":true,"result":"3","result_reference":null,'
        '"error":null,"traceback":null,"exception_type":null,"retryable":null}',
        completion_success=True,
        completion_error=None,
    )
    assert type(encoded_subclass) is not str

    monkeypatch.setattr(
        target_codec, "decode_target_execution_request", lambda _raw, **_kw: fake_request
    )
    monkeypatch.setattr(target_probe, "verify_ray_target_execution", lambda _req: observed_target)
    monkeypatch.setattr(
        entrypoint_module,
        "execute_task",
        lambda *_a, **_kw: encoded_subclass,
    )
    captured_completion: list[Any] = []
    monkeypatch.setattr(
        target_codec,
        "encode_target_execution_result",
        lambda comp: captured_completion.append(comp) or '{"encoded":true}',
    )

    out = remote_module._execute_target_bound_django_task_remote(
        "raw-request",
        expected_task_execution_pk=7,
        expected_task_id="proto2-subclass-test",
        expected_attempt_number=1,
        expected_execution_generation=1,
        expected_target_execution_evidence_id=1,
        expected_target_execution_evidence_digest="a" * 64,
        expected_target_execution_claimed_at=claimed_at,
        expected_target_expectation_digest="b" * 64,
        expected_claim_attestation_digest="c" * 64,
    )
    assert out == '{"encoded":true}'
    assert len(captured_completion) == 1
    assert captured_completion[0].application_completion.success is True
    assert captured_completion[0].application_completion.result == "3"


def test_dsl_cancellation_scope_fallback_matching_and_executor_edge_branches(
    settings: Any,
) -> None:
    """Cover workflow/dsl.py helper branches: nested scopes, RayTaskError causes, map/group edges."""
    from django_ray.execution_codec import NestedExecutionRequestRejected
    from django_ray.workflow import dsl as dsl_mod
    from django_ray.workflow.plans import WorkflowPlanMismatchError

    settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}

    # _DslCancellationScope nesting, register_refs propagation, cancel/check_cancelled
    parent_scope = dsl_mod._DslCancellationScope()
    child_scope = dsl_mod._DslCancellationScope(parent=parent_scope)
    child_scope.register_refs(())
    child_scope.register_refs(["r1"])
    assert parent_scope.snapshot_refs() == ["r1"]
    parent_scope.cancel()
    assert child_scope.is_cancelled is True
    with pytest.raises(dsl_mod._DslCancelledError):
        child_scope.check_cancelled()

    # _match_fallback_exception: control errors & RayTaskError wrapping control/app causes
    class RayTaskError(Exception):
        def __init__(self, cause: BaseException | None = None) -> None:
            super().__init__("ray task error")
            self.cause = cause

    ctrl_err = WorkflowPlanMismatchError("stale")
    assert dsl_mod._match_fallback_exception(ctrl_err, (Exception,)) is None
    assert dsl_mod._match_fallback_exception(RayTaskError(cause=ctrl_err), (Exception,)) is None
    nested_rej = NestedExecutionRequestRejected("rejected")
    ray_dunder = RayTaskError(cause=None)
    ray_dunder.__cause__ = nested_rej
    assert dsl_mod._match_fallback_exception(ray_dunder, (Exception,)) is None

    val_err = ValueError("match me")
    ray_dunder_val = RayTaskError(cause=None)
    ray_dunder_val.__cause__ = val_err
    assert dsl_mod._match_fallback_exception(ray_dunder_val, (ValueError,)) is val_err
    assert (
        dsl_mod._match_fallback_exception(RayTaskError(cause=KeyError("k")), (ValueError,)) is None
    )

    # _can_submit_directly_on_executor returns False for non-direct nodes (Fallback)
    fb_node = WorkflowExpr(_double).with_fallback(_fallback_recover).node
    assert dsl_mod._can_submit_directly_on_executor(fb_node) is False
    with pytest.raises(TypeError, match="Expected a WorkflowSignature"):
        dsl_mod._submit_direct_on_executor(
            fb_node, (1,), {}, executor=object(), node_id="0", dependencies=()
        )

    # _cancel_executor_refs empty, all-resolved, and exception-swallowing paths
    dsl_mod._cancel_executor_refs(object(), [], timeout_seconds=1.0)
    dsl_mod._cancel_executor_refs(
        SimpleNamespace(_unresolved_cleanup_refs=lambda _refs: []),
        ["r1"],
        timeout_seconds=1.0,
    )
    dsl_mod._cancel_executor_refs(
        SimpleNamespace(
            cancel_and_drain_fold_payloads=lambda _r, **_kw: (_ for _ in ()).throw(
                RuntimeError("cancel err")
            )
        ),
        ["r1"],
        timeout_seconds=1.0,
    )

    # _execute_signature_on_executor without capture_cleanup and cancelled mid-flight
    class _MinimalExec:
        def submit_step(self, sig: Any, args: Any, kwargs: Any, _nid: str, _deps: Any) -> Any:
            return ("submitted", args)

        def track_cleanup(self, _v: Any) -> None:
            pass

        def resolve(self, value: Any) -> Any:
            return value[1][0] * 2

    sig = workflows_module.step(_double)
    assert (
        dsl_mod._execute_signature_on_executor(sig, (5,), {}, executor=_MinimalExec(), node_id="0")
        == 10
    )

    pre_cancelled = dsl_mod._DslCancellationScope()
    pre_cancelled.cancel()
    with pytest.raises(dsl_mod._DslCancelledError):
        dsl_mod._execute_signature_on_executor(
            sig, (5,), {}, executor=_MinimalExec(), node_id="0", cancel_scope=pre_cancelled
        )

    # Cancel scope triggered between submit and resolve
    mid_scope = dsl_mod._DslCancellationScope()

    class _CancellingSubmitExec(_MinimalExec):
        def submit_step(self, sig: Any, args: Any, kwargs: Any, _nid: str, _deps: Any) -> Any:
            mid_scope.cancel()
            return ("submitted", args)

    with pytest.raises(dsl_mod._DslCancelledError):
        dsl_mod._execute_signature_on_executor(
            sig, (5,), {}, executor=_CancellingSubmitExec(), node_id="0", cancel_scope=mid_scope
        )

    # Direct concurrent DSL group and direct concurrent DSL map on minimal non-local executor
    class _DirectNonLocalExec:
        def submit_step(self, sig: Any, args: Any, kwargs: Any, _nid: str, _deps: Any) -> Any:
            return args[0] + 1

        def track_cleanup(self, _v: Any) -> None:
            pass

        def collect(self, values: list[Any]) -> list[Any]:
            return values

        def resolve(self, value: Any) -> Any:
            return value

        def wait_one(self, values: list[Any]) -> int:
            return 0

        def resolve_ready(self, value: Any) -> Any:
            return value

    grp_node = dsl_mod._DslGroupNode(children=(sig, sig))
    assert dsl_mod._execute_dsl_group_concurrent(
        grp_node, (4,), {}, executor=_DirectNonLocalExec(), node_id="0"
    ) == [5, 5]

    map_node = dsl_mod._DslMapNode(body=sig, max_concurrency=2, max_items=3)
    assert dsl_mod._execute_dsl_map_concurrent(
        map_node, [1, 2], executor=_DirectNonLocalExec(), node_id="0"
    ) == [2, 3]

    # Generator exceeding max_items in direct concurrent map closes iterator cleanly
    def _too_many() -> Any:
        yield from [1, 2, 3, 4]

    with pytest.raises(workflows_module.WorkflowDefinitionError, match="max_items"):
        dsl_mod._execute_dsl_map_concurrent(
            map_node, _too_many(), executor=_DirectNonLocalExec(), node_id="0"
        )


def test_target_probe_compatibility_classification_and_result_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover target/probe.py compatibility reasons, result semantics, and verify_ray_target_execution."""
    import sys
    from datetime import UTC, datetime, timedelta

    from django_ray.execution_codec import ExecutionIdentity
    from django_ray.target import attestation as att
    from django_ray.target import execution_codec as tc
    from django_ray.target import probe as probe_mod

    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    runtime = att.RayRuntimeVersion(
        ray_major=2,
        ray_minor=60,
        ray_patch=0,
        python_implementation="cpython",
        python_major=3,
        python_minor=12,
        python_patch=0,
    )
    expectation = att.RayTargetExpectation(
        target_key="default",
        policy_revision=1,
        runner_family=att.RayRunnerFamily.RAY_CORE,
        cluster_session="session_alpha",
        runtime=runtime,
    )
    node_id = "1" * 56
    boundary = att.build_ray_observation_boundary(
        resource_state_version_before=1,
        resource_state_version_after=1,
        node_state_versions_before=(
            att.RayNodeStateVersion(node_id=node_id, node_state_version=1),
        ),
        node_state_versions_after=(att.RayNodeStateVersion(node_id=node_id, node_state_version=1),),
    )
    node_obs = att.build_ray_node_observation(
        node_id=node_id,
        cluster_session="session_alpha",
        runtime=runtime,
    )
    attestation = att.build_ray_cluster_attestation(
        expectation=expectation,
        boundary=boundary,
        nodes=(node_obs,),
        observed_at=now,
        expires_at=now + timedelta(seconds=60),
    )
    identity = ExecutionIdentity(
        task_execution_pk=1,
        task_id="probe-sem-1",
        attempt_number=1,
        execution_generation=1,
    )
    exp_digest = att.ray_target_expectation_digest(expectation)
    att_digest = att.ray_cluster_attestation_digest(attestation)
    ev_digest = "sha256:" + ("d" * 64)
    claim_at = now + timedelta(seconds=5)
    obs_at = now + timedelta(seconds=10)

    def _make_obs(
        *,
        session: str = "session_alpha",
        rt: att.RayRuntimeVersion = runtime,
        nid: str = node_id,
        membership_digest: str = attestation.membership_digest,
        at: datetime = obs_at,
    ) -> tc.TargetExecutionObservedEvidence:
        return tc.build_target_execution_observed_evidence(
            identity=identity,
            target_execution_evidence_id=1,
            target_execution_evidence_digest=ev_digest,
            target_execution_claimed_at=claim_at,
            target_expectation_digest=exp_digest,
            claim_attestation_digest=att_digest,
            observed_node_id=nid,
            observed_cluster_session=session,
            observed_runtime=rt,
            observed_membership_digest=membership_digest,
            observed_at=at,
        )

    # All 7 compatibility mismatch classifications + compatible (None)
    assert (
        probe_mod._classify_ray_target_execution_compatibility(
            expectation=expectation, attestation=attestation, observed_target=_make_obs()
        )
        is None
    )
    assert (
        probe_mod._classify_ray_target_execution_compatibility(
            expectation=expectation,
            attestation=attestation,
            observed_target=_make_obs(at=now + timedelta(seconds=120)),
        )
        is tc.TargetExecutionCompatibilityReason.EXPIRED
    )
    assert (
        probe_mod._classify_ray_target_execution_compatibility(
            expectation=expectation,
            attestation=attestation,
            observed_target=_make_obs(session="session_other"),
        )
        is tc.TargetExecutionCompatibilityReason.CLUSTER_SESSION_MISMATCH
    )
    assert (
        probe_mod._classify_ray_target_execution_compatibility(
            expectation=expectation,
            attestation=attestation,
            observed_target=_make_obs(rt=att.RayRuntimeVersion(2, 61, 0, "cpython", 3, 12, 0)),
        )
        is tc.TargetExecutionCompatibilityReason.RAY_VERSION_MISMATCH
    )
    assert (
        probe_mod._classify_ray_target_execution_compatibility(
            expectation=expectation,
            attestation=attestation,
            observed_target=_make_obs(rt=att.RayRuntimeVersion(2, 60, 0, "pypy", 3, 12, 0)),
        )
        is tc.TargetExecutionCompatibilityReason.PYTHON_IMPLEMENTATION_MISMATCH
    )
    assert (
        probe_mod._classify_ray_target_execution_compatibility(
            expectation=expectation,
            attestation=attestation,
            observed_target=_make_obs(rt=att.RayRuntimeVersion(2, 60, 0, "cpython", 3, 13, 0)),
        )
        is tc.TargetExecutionCompatibilityReason.PYTHON_VERSION_MISMATCH
    )
    assert (
        probe_mod._classify_ray_target_execution_compatibility(
            expectation=expectation,
            attestation=attestation,
            observed_target=_make_obs(nid="2" * 56),
        )
        is tc.TargetExecutionCompatibilityReason.CURRENT_NODE_NOT_ATTESTED
    )
    assert (
        probe_mod._classify_ray_target_execution_compatibility(
            expectation=expectation,
            attestation=attestation,
            observed_target=_make_obs(membership_digest="sha256:" + ("f" * 64)),
        )
        is tc.TargetExecutionCompatibilityReason.MEMBERSHIP_MISMATCH
    )

    # validate_ray_target_execution_result_semantics: valid completion, valid rejection, and errors
    app_comp = tc.TargetApplicationCompletion(
        success=True,
        result='"ok"',
        result_reference=None,
        error=None,
        traceback=None,
        exception_type=None,
        retryable=None,
    )
    valid_comp = tc.TargetExecutionCompletion(
        identity=identity,
        execution_protocol_version=2,
        target_execution_evidence_id=1,
        target_execution_evidence_digest=ev_digest,
        target_execution_claimed_at=claim_at,
        target_expectation_digest=exp_digest,
        claim_attestation_digest=att_digest,
        executor_django_ray_version="0.8.0",
        observed_target=_make_obs(),
        application_completion=app_comp,
    )
    probe_mod.validate_ray_target_execution_result_semantics(
        valid_comp,
        target_expectation=expectation,
        claim_attestation=attestation,
        validation_now=now + timedelta(seconds=15),
    )

    valid_rej = tc.TargetExecutionCompatibilityRejection(
        identity=identity,
        execution_protocol_version=2,
        target_execution_evidence_id=1,
        target_execution_evidence_digest=ev_digest,
        target_execution_claimed_at=claim_at,
        target_expectation_digest=exp_digest,
        claim_attestation_digest=att_digest,
        executor_django_ray_version="0.8.0",
        observed_target=_make_obs(session="session_other"),
        compatibility_reason=tc.TargetExecutionCompatibilityReason.CLUSTER_SESSION_MISMATCH,
    )
    probe_mod.validate_ray_target_execution_result_semantics(
        valid_rej,
        target_expectation=expectation,
        claim_attestation=attestation,
        validation_now=now + timedelta(seconds=15),
    )

    # Invalid type, naive receipt time, and mismatched rejection reason fail closed
    with pytest.raises(probe_mod.RayTargetExecutionResultValidationError):
        probe_mod.validate_ray_target_execution_result_semantics(
            object(),  # type: ignore[arg-type]
            target_expectation=expectation,
            claim_attestation=attestation,
        )
    with pytest.raises(probe_mod.RayTargetExecutionResultValidationError):
        probe_mod.validate_ray_target_execution_result_semantics(
            valid_comp,
            target_expectation=expectation,
            claim_attestation=attestation,
            validation_now=datetime(2026, 6, 1, 12, 0, 15),
        )
    with pytest.raises(probe_mod.RayTargetExecutionResultValidationError):
        probe_mod.validate_ray_target_execution_result_semantics(
            valid_comp,
            target_expectation=expectation,
            claim_attestation=attestation,
            validation_now=now + timedelta(seconds=6),
        )
    wrong_rej = tc.TargetExecutionCompatibilityRejection(
        identity=identity,
        execution_protocol_version=2,
        target_execution_evidence_id=1,
        target_execution_evidence_digest=ev_digest,
        target_execution_claimed_at=claim_at,
        target_expectation_digest=exp_digest,
        claim_attestation_digest=att_digest,
        executor_django_ray_version="0.8.0",
        observed_target=_make_obs(session="session_other"),
        compatibility_reason=tc.TargetExecutionCompatibilityReason.EXPIRED,
    )
    with pytest.raises(probe_mod.RayTargetExecutionResultValidationError):
        probe_mod.validate_ray_target_execution_result_semantics(
            wrong_rej,
            target_expectation=expectation,
            claim_attestation=attestation,
            validation_now=now + timedelta(seconds=15),
        )

    # verify_ray_target_execution error & success paths with mocked Ray + snapshot
    with pytest.raises(probe_mod.RayTargetProbeError):
        probe_mod.verify_ray_target_execution(object())  # type: ignore[arg-type]

    request = tc.TargetExecutionRequest(
        transport_version=1,
        identity=identity,
        execution_protocol_version=2,
        callable_path="testproject.tasks.add_numbers",
        serialized_args="[]",
        serialized_kwargs="{}",
        input_reference=None,
        runtime_env_profile=None,
        runtime_env_hash="",
        runtime_env_plan_identity=None,
        compiled_graph_submission_transport=None,
        target_execution_evidence_id=1,
        target_execution_evidence_digest=ev_digest,
        target_execution_claimed_at=claim_at,
        target_expectation_digest=exp_digest,
        claim_attestation_digest=att_digest,
        target_expectation=expectation,
        claim_attestation=attestation,
    )
    monkeypatch.setitem(
        sys.modules,
        "ray",
        SimpleNamespace(__version__="1.0.0", is_initialized=lambda: True),
    )
    with pytest.raises(probe_mod.RayTargetProbeError):
        probe_mod.verify_ray_target_execution(request)

    monkeypatch.setitem(
        sys.modules,
        "ray",
        SimpleNamespace(
            __version__=probe_mod.RAY_TARGET_PROBE_RAY_VERSION,
            is_initialized=lambda: False,
        ),
    )
    with pytest.raises(probe_mod.RayTargetProbeError):
        probe_mod.verify_ray_target_execution(request)

    monkeypatch.setitem(
        sys.modules,
        "ray",
        SimpleNamespace(
            __version__=probe_mod.RAY_TARGET_PROBE_RAY_VERSION,
            is_initialized=lambda: True,
        ),
    )
    caller_obs = SimpleNamespace(
        node_id=node_id,
        session_name="session_alpha",
        python_implementation="cpython",
        python_version=(3, 12, 0),
    )
    snap_mismatch = SimpleNamespace(
        session_name="session_different",
        cluster_resource_state_version=1,
        node_state_versions=((node_id, 1),),
    )
    monkeypatch.setattr(probe_mod, "_current_caller_observation", lambda _r: caller_obs)
    monkeypatch.setattr(
        probe_mod, "_current_resource_state_snapshot", lambda _r, **_kw: snap_mismatch
    )
    with pytest.raises(probe_mod.RayTargetProbeError):
        probe_mod.verify_ray_target_execution(request)

    # Matching session but expired clock triggers RayTargetExecutionCompatibilityError
    snap_ok = SimpleNamespace(
        session_name="session_alpha",
        cluster_resource_state_version=1,
        node_state_versions=((node_id, 1),),
    )
    monkeypatch.setattr(probe_mod, "_current_resource_state_snapshot", lambda _r, **_kw: snap_ok)

    class _FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return now + timedelta(seconds=120)

    monkeypatch.setattr(probe_mod, "datetime", _FixedNow)
    with pytest.raises(probe_mod.RayTargetExecutionCompatibilityError) as compat_exc:
        probe_mod.verify_ray_target_execution(request)
    assert compat_exc.value.reason is tc.TargetExecutionCompatibilityReason.EXPIRED

    class _ValidNow(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return now + timedelta(seconds=10)

    monkeypatch.setattr(probe_mod, "datetime", _ValidNow)
    verified = probe_mod.verify_ray_target_execution(request)
    assert verified.observed_node_id == node_id
    assert verified.observed_cluster_session == "session_alpha"


def test_distributed_helpers_validation_strict_enforcement_and_session_edges(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    """Cover runtime/distributed.py validation, strict leaf context, async loop, and helper edges."""
    import asyncio
    import sys

    from django.apps import apps

    from django_ray.execution_codec import (
        NestedExecutionBoundaryKind,
        NestedExecutionRequestRejected,
    )
    from django_ray.runtime import distributed as dist

    # _close_old_django_connections when apps.ready is False and on error
    monkeypatch.setattr(apps, "ready", False)
    dist._close_old_django_connections()
    monkeypatch.setattr(apps, "ready", True)

    # _invoke_callable rejects coroutine function / awaitable when an event loop is already running
    async def _async_add(x: int) -> int:
        return x + 1

    def _returns_coro(x: int) -> Any:
        return _async_add(x)

    async def _run_inside_loop() -> None:
        with pytest.raises(RuntimeError, match="running event loop"):
            dist._invoke_callable(_async_add, 1)
        with pytest.raises(RuntimeError, match="running event loop"):
            dist._invoke_callable(_returns_coro, 1)

    asyncio.run(_run_inside_loop())
    assert dist._invoke_callable(_async_add, 4) == 5
    assert dist._invoke_callable(_returns_coro, 6) == 7

    # _RemoteCalls bounds & iteration + _NestedCallableBindingCache cache hit
    rc = dist._RemoteCalls(2, lambda idx: (idx, idx * 2))
    assert list(rc) == [(0, 0), (1, 2)]
    with pytest.raises(IndexError):
        _ = rc[5]
    cache = dist._NestedCallableBindingCache()
    payload = b"callable-bytes"
    first_digest = cache.get(payload)
    assert cache.get(payload) == first_digest

    # Input validation helpers
    with pytest.raises(TypeError, match="num_cpus"):
        dist._validate_resources(True, 0.0)
    with pytest.raises(ValueError, match="num_gpus"):
        dist._validate_resources(1.0, float("nan"))
    with pytest.raises(TypeError, match="max_concurrency"):
        dist._validate_max_concurrency(True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_concurrency"):
        dist._validate_max_concurrency(0)
    with pytest.raises(TypeError, match="items"):
        dist._materialize_items("not-a-list", "items")
    with pytest.raises(TypeError, match="func"):
        dist._validate_callable(123)

    # _is_strict_nested_request_enforced across env vars, settings, and malformed job config
    for env_key in (
        "DJANGO_RAY_REQUIRE_STRICT_NESTED_REQUEST",
        "DJANGO_RAY_ALLOW_LEGACY_NESTED_CALLABLES",
        "RAY_JOB_CONFIG_JSON_ENV_VAR",
    ):
        monkeypatch.delenv(env_key, raising=False)
    settings.DJANGO_RAY = {}
    assert dist._is_strict_nested_request_enforced() is False

    monkeypatch.setenv("DJANGO_RAY_REQUIRE_STRICT_NESTED_REQUEST", "true")
    assert dist._is_strict_nested_request_enforced() is True
    monkeypatch.delenv("DJANGO_RAY_REQUIRE_STRICT_NESTED_REQUEST")

    monkeypatch.setenv("DJANGO_RAY_ALLOW_LEGACY_NESTED_CALLABLES", "false")
    assert dist._is_strict_nested_request_enforced() is True
    monkeypatch.delenv("DJANGO_RAY_ALLOW_LEGACY_NESTED_CALLABLES")

    settings.DJANGO_RAY = {"REQUIRE_STRICT_NESTED_REQUEST": True}
    assert dist._is_strict_nested_request_enforced() is True
    settings.DJANGO_RAY = {}

    monkeypatch.setenv("RAY_JOB_CONFIG_JSON_ENV_VAR", "{malformed-json")
    assert dist._is_strict_nested_request_enforced() is True
    with pytest.raises(NestedExecutionRequestRejected):
        dist._assert_active_leaf_context(strict_expected=False)
    monkeypatch.delenv("RAY_JOB_CONFIG_JSON_ENV_VAR")

    with pytest.raises(NestedExecutionRequestRejected):
        dist._assert_active_leaf_context(strict_expected=True)

    # Partial strict controls rejected before unpickling
    with pytest.raises(NestedExecutionRequestRejected):
        with dist._nested_distributed_leaf_execution(
            b"not-unpickled",
            "versioned-token",
            1,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            boundary_kind=NestedExecutionBoundaryKind.DISTRIBUTED_MAP,
        ):
            pass

    # _get_cached_remote unknown kind + _maybe_put_shared + cluster helpers with fake Ray
    fake_ray = SimpleNamespace(
        is_initialized=lambda: True,
        put=lambda v: ("put", v),
        cluster_resources=lambda: {
            "CPU": 8.0,
            "node:10.0.0.1": 1.0,
            "node:10.0.0.2": 1.0,
            "node:__internal_head__": 1.0,
        },
        remote=lambda fn: fn,
    )
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    with pytest.raises(ValueError, match="unknown distributed helper"):
        dist._get_cached_remote("invalid")
    assert dist._maybe_put_shared(fake_ray, "x", item_count=1) == "x"
    assert dist._maybe_put_shared(fake_ray, "x", item_count=2) == ("put", "x")
    assert dist.get_ray_resources()["CPU"] == 8.0
    assert dist.get_num_workers() == 2
    assert dist.get_total_cpus() == 8.0

    # Validation errors on parallel_starmap and scatter_gather
    with pytest.raises(TypeError, match="tuple of positional arguments"):
        dist.parallel_starmap(_double, [1])  # type: ignore[list-item]
    with pytest.raises(TypeError, match="tuple"):
        dist.scatter_gather([(_double, ())])  # type: ignore[list-item]
    with pytest.raises(TypeError, match="positional arguments"):
        dist.scatter_gather([(_double, [1], {})])  # type: ignore[list-item]
    with pytest.raises(TypeError, match="keyword arguments"):
        dist.scatter_gather([(_double, (1,), [])])  # type: ignore[list-item]
