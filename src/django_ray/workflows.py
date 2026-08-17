"""Composable Ray-native workflows with a single durable task boundary.

Workflow steps are submitted directly to Ray and do not create
``RayTaskExecution`` rows. Call a workflow from an ordinary Django task when
the complete workflow needs durable queueing, retries, and result storage.
"""

from __future__ import annotations

import json
import math
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence, Sized
from contextlib import AbstractContextManager, contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.runtime.import_utils import import_callable
from django_ray.workflow.contracts import WorkflowDefinitionKind


class WorkflowDefinitionError(ValueError):
    """Raised when a workflow signature cannot be constructed or executed."""


class _FrozenMapping(Mapping[Any, Any]):
    """Pickle-safe read-only mapping used by reusable workflow signatures."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[Any, Any]) -> None:
        self._values = dict(values)

    def __getitem__(self, key: Any) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __reduce__(self) -> tuple[Any, tuple[dict[Any, Any]]]:
        return type(self), (dict(self._values),)

    def __deepcopy__(self, memo: dict[int, Any]) -> _FrozenMapping:
        del memo
        return self

    def __repr__(self) -> str:
        return f"_FrozenMapping({self._values!r})"


@dataclass(frozen=True)
class _Submission:
    """A submitted value plus the graph nodes that produce it."""

    value: Any
    terminal_node_ids: tuple[str, ...]


def _callable_path(callable_obj: Callable[..., Any] | str) -> str:
    if isinstance(callable_obj, str):
        if "." not in callable_obj:
            raise WorkflowDefinitionError(
                f"Invalid callable path '{callable_obj}': expected a dotted import path"
            )
        return callable_obj

    module_path = getattr(callable_obj, "module_path", None)
    if module_path:
        return str(module_path)

    underlying = getattr(callable_obj, "func", callable_obj)
    module = getattr(underlying, "__module__", None)
    name = getattr(underlying, "__name__", None)
    qualname = getattr(underlying, "__qualname__", name)
    if not module or not name or (qualname and "<locals>" in qualname):
        raise WorkflowDefinitionError("Workflow steps must be importable module-level callables")
    if qualname != name:
        raise WorkflowDefinitionError(
            "Workflow steps must currently be module-level functions, not methods"
        )
    return f"{module}.{name}"


def _json_safe(value: Any) -> Any:
    """Make scheduling metadata safe for the durable progress snapshot."""
    return json.loads(json.dumps(_thaw_definition_value(value), default=str))


def _clone_runtime_env(
    runtime_env: str | Mapping[str, Any] | None,
) -> str | dict[str, Any] | None:
    if isinstance(runtime_env, Mapping):
        return _thaw_definition_value(runtime_env)
    return runtime_env


def _freeze_definition_value(value: Any) -> Any:
    """Deep-freeze plan-relevant builder metadata."""
    if isinstance(value, Mapping):
        return _FrozenMapping({key: _freeze_definition_value(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_definition_value(item) for item in value)
    return deepcopy(value)


def _thaw_definition_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_definition_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_definition_value(item) for item in value]
    return deepcopy(value)


def report_progress(
    current: int | float,
    total: int | float,
    *,
    message: str | None = None,
    metrics: dict[str, Any] | None = None,
) -> bool:
    """Report application-level progress from a running workflow step."""
    from django_ray.runtime.context import report_workflow_progress

    return report_workflow_progress(
        current,
        total,
        message=message,
        metrics=metrics,
    )


class _Executor(ABC):
    def bind_plan(
        self,
        materialized_plan: Any,
        *,
        requested_policy: str,
        reporting_policy: str = "full",
    ) -> None:
        """Attach a pre-submit snapshot to an executor implementation."""
        self.materialized_plan = materialized_plan

    @abstractmethod
    def submit_step(
        self,
        signature: Step,
        input_args: tuple[Any, ...],
        input_kwargs: dict[str, Any],
        node_id: str,
        dependencies: tuple[str, ...],
    ) -> Any:
        """Submit one step and return a value or future."""

    @abstractmethod
    def collect(self, values: list[Any]) -> Any:
        """Return one future/value containing an ordered result list."""

    @abstractmethod
    def resolve(self, value: Any) -> Any:
        """Resolve a future, or return an immediate value."""

    def resolve_ready(self, value: Any) -> Any:
        """Resolve a value already returned as ready by ``wait_one``."""
        return self.resolve(value)

    @contextmanager
    def capture_cleanup(self, values: list[Any]) -> Iterator[None]:
        """Capture physical refs submitted for one bounded logical item."""
        stack = getattr(self, "_cleanup_capture_stack", None)
        if stack is None:
            stack = []
            self._cleanup_capture_stack = stack
        stack.append(values)
        try:
            yield
        finally:
            popped = stack.pop()
            assert popped is values

    def track_cleanup(self, value: Any) -> None:
        """Record a physical ref in the innermost active item capture."""
        stack = getattr(self, "_cleanup_capture_stack", None)
        if stack:
            stack[-1].append(value)

    def store(self, value: Any) -> Any:
        """Return one future/value containing an already resolved value."""
        return value

    def wait_one(self, values: Sequence[Any]) -> int:
        """Wait for one value and return its index in ``values``."""
        return 0

    def start_result_buffer(
        self,
        *,
        max_items: int,
        max_serialized_bytes: int,
        actor_options: Mapping[str, Any],
    ) -> Any | None:
        """Reserve a Ray result buffer, or return ``None`` for local execution."""
        return None

    def wait_result_buffer_leaf(self, values: Sequence[Any]) -> int:
        """Select one ready leaf without resolving its payload in the coordinator."""
        return self.wait_one(values)

    def append_result_buffer(
        self,
        buffer: Any,
        *,
        index: int,
        value: Any,
    ) -> None:
        """Append one leaf through the small-acknowledgement actor protocol."""
        raise NotImplementedError

    def finalize_result_buffer(self, buffer: Any, *, expected_items: int) -> Any:
        """Return the unresolved ordered payload from a finalized Ray buffer."""
        raise NotImplementedError

    def discard_result_buffer(self, buffer: Any, *, timeout_seconds: float) -> None:
        """Best-effort cleanup for a failed or cancelled result-buffer map."""
        return None

    def start_result_fold(
        self,
        *,
        max_items: int,
        max_concurrency: int,
        max_serialized_bytes: int,
        actor_options: Mapping[str, Any],
        reducer: Step,
        reducer_node_id: str,
        initial: Any,
    ) -> Any | None:
        """Reserve a Ray result-fold actor, or return ``None`` locally."""
        del (
            max_items,
            max_concurrency,
            max_serialized_bytes,
            actor_options,
            reducer_node_id,
            initial,
        )
        if reducer.bootstrap_django:
            from django_ray.runtime.entrypoint import bootstrap_django

            bootstrap_django()
        reducer_callable = import_callable(reducer.callable_path)
        import inspect

        if (
            inspect.iscoroutinefunction(reducer_callable)
            or inspect.isgeneratorfunction(reducer_callable)
            or inspect.isasyncgenfunction(reducer_callable)
        ):
            raise WorkflowDefinitionError(
                "reduce requires a synchronous non-generator reducer callable"
            )
        return None

    def wait_result_fold_leaf(self, values: Sequence[Any]) -> int:
        """Select a ready fold leaf without resolving it in the coordinator."""
        return self.wait_one(values)

    def append_result_fold(
        self,
        fold: Any,
        *,
        index: int,
        value: Any,
    ) -> int:
        """Return the total number of items incorporated by the ordered fold."""
        raise NotImplementedError

    def finalize_result_fold(self, fold: Any, *, expected_items: int) -> Any:
        """Return the unresolved accumulator from a finalized Ray fold."""
        raise NotImplementedError

    def discard_result_fold(self, fold: Any, *, timeout_seconds: float) -> None:
        """Best-effort cleanup for a failed or cancelled result-fold map."""
        return None

    def reduce_local(self, signature: Step, accumulator: Any, item: Any) -> Any:
        """Apply a reducer Step directly for actor-free local execution."""
        if signature.bootstrap_django:
            from django_ray.runtime.entrypoint import bootstrap_django

            bootstrap_django()
        callable_obj = import_callable(signature.callable_path)
        value = callable_obj(
            accumulator,
            item,
            *signature.bound_args,
            **signature.bound_kwargs,
        )
        from django_ray.runtime.result_fold import validate_result_fold_value

        return validate_result_fold_value(value)

    def cancel_and_drain_fold_payloads(
        self,
        values: Sequence[Any],
        *,
        timeout_seconds: float,
    ) -> None:
        """Best-effort payload-safe cleanup for failed result folds."""
        return None

    def cancel_and_drain(
        self,
        values: Sequence[Any],
        *,
        timeout_seconds: float,
    ) -> None:
        """Best-effort cleanup for submitted values after map failure."""
        return None

    def suppress_progress(self) -> AbstractContextManager[None]:
        """Suppress physical leaf nodes while an aggregate map node is active."""
        return nullcontext()

    def map_started(
        self,
        node_id: str,
        label: str,
        dependencies: tuple[str, ...],
        *,
        max_concurrency: int | None,
        max_items: int | None,
    ) -> None:
        """Register one aggregate bounded-map node when observability is active."""
        return None

    def map_progress(
        self,
        node_id: str,
        label: str,
        *,
        submitted: int,
        completed: int,
        input_exhausted: bool,
        force: bool = False,
    ) -> None:
        """Update bounded-map aggregate counters."""
        return None

    def map_finished(
        self,
        node_id: str,
        label: str,
        *,
        submitted: int,
        completed: int,
        input_exhausted: bool,
        failed: bool = False,
        error: str | None = None,
    ) -> None:
        """Mark a bounded-map aggregate node terminal."""
        return None

    def finish_progress(self, *, failed: bool = False) -> None:
        """Flush any final workflow progress state."""
        return None


class _LocalExecutor(_Executor):
    def __init__(self, materialized_plan: Any | None = None) -> None:
        self.materialized_plan = materialized_plan
        if materialized_plan is not None:
            self.bind_plan(
                materialized_plan,
                requested_policy="local",
                reporting_policy="disabled",
            )

    def bind_plan(
        self,
        materialized_plan: Any,
        *,
        requested_policy: str,
        reporting_policy: str = "full",
    ) -> None:
        super().bind_plan(
            materialized_plan,
            requested_policy=requested_policy,
            reporting_policy=reporting_policy,
        )
        from django_ray.runtime.context import get_current_task_context

        task_context = get_current_task_context()
        if task_context is None:
            return
        from django_ray.workflow.progress.runs import pin_workflow_plan

        selection = materialized_plan.plan.eligibility.select(
            "local",
            requested_policy=requested_policy,
            reporting_policy="disabled",
        )
        if task_context.attempt_number is None or task_context.execution_generation is None:
            return
        if not pin_workflow_plan(task_context, materialized_plan.plan, selection):
            from django_ray.workflow.plans import WorkflowPlanMismatchError

            raise WorkflowPlanMismatchError(
                "The durable task attempt is stale; workflow plan pinning was rejected"
            )

    def submit_step(
        self,
        signature: Step,
        input_args: tuple[Any, ...],
        input_kwargs: dict[str, Any],
        node_id: str,
        dependencies: tuple[str, ...],
    ) -> Any:
        if signature.bootstrap_django:
            from django_ray.runtime.entrypoint import bootstrap_django

            bootstrap_django()
        callable_obj = import_callable(signature.callable_path)
        kwargs = {**input_kwargs, **signature.bound_kwargs}
        return callable_obj(*input_args, *signature.bound_args, **kwargs)

    def collect(self, values: list[Any]) -> list[Any]:
        return values

    def resolve(self, value: Any) -> Any:
        return value


_execute_workflow_step_remote_cached = None
_collect_workflow_results_remote_cached = None
_workflow_progress_actor_cached = None
_workflow_result_buffer_actor_cached = None
_workflow_result_fold_actor_cached = None


def _get_cached_workflow_remotes() -> tuple[Any, Any, Any]:
    global _execute_workflow_step_remote_cached
    global _collect_workflow_results_remote_cached
    global _workflow_progress_actor_cached

    if _execute_workflow_step_remote_cached is None:
        import ray

        from django_ray.runtime.remote import (
            WorkflowProgressActor,
            collect_workflow_results_remote,
            execute_workflow_step_remote,
        )

        _execute_workflow_step_remote_cached = ray.remote(execute_workflow_step_remote)
        _collect_workflow_results_remote_cached = ray.remote(collect_workflow_results_remote)
        _workflow_progress_actor_cached = ray.remote(num_cpus=0)(WorkflowProgressActor)

    return (
        _execute_workflow_step_remote_cached,
        _collect_workflow_results_remote_cached,
        _workflow_progress_actor_cached,
    )


def _get_cached_result_buffer_actor() -> Any:
    global _workflow_result_buffer_actor_cached

    if _workflow_result_buffer_actor_cached is None:
        import ray

        from django_ray.runtime.result_buffer import WorkflowMapResultBuffer

        _workflow_result_buffer_actor_cached = ray.remote(WorkflowMapResultBuffer)
    return _workflow_result_buffer_actor_cached


def _get_cached_result_fold_actor() -> Any:
    global _workflow_result_fold_actor_cached

    if _workflow_result_fold_actor_cached is None:
        import ray

        from django_ray.runtime.result_fold import WorkflowMapResultFold

        _workflow_result_fold_actor_cached = ray.remote(WorkflowMapResultFold)
    return _workflow_result_fold_actor_cached


@dataclass
class _RayResultBufferSession:
    """One live buffer actor owned by this workflow coordinator."""

    actor: Any
    closed: bool = False


@dataclass
class _RayResultFoldSession:
    """One live ordered-fold actor owned by this workflow coordinator."""

    actor: Any
    maximum_items: int
    maximum_concurrency: int
    maximum_out_of_order_items: int
    maximum_serialized_bytes: int
    folded_items: int = 0
    closed: bool = False


def _failed_snapshot_has_causally_complete_ancestors(snapshot: Mapping[str, Any]) -> bool:
    """Require every dependency of an observed failed node to be succeeded."""

    graph = snapshot.get("graph")
    if not isinstance(graph, Mapping):
        return False
    raw_nodes = graph.get("nodes")
    raw_edges = graph.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        return False

    states_by_node: dict[str, str] = {}
    for item in raw_nodes:
        if not isinstance(item, Mapping):
            return False
        node_id = item.get("node_id")
        state = item.get("state")
        if (
            not isinstance(node_id, str)
            or not node_id
            or node_id in states_by_node
            or not isinstance(state, str)
            or state not in {"PENDING", "RUNNING", "SUCCEEDED", "FAILED"}
        ):
            return False
        states_by_node[node_id] = state

    failed_node_ids = {node_id for node_id, state in states_by_node.items() if state == "FAILED"}
    if not failed_node_ids:
        return False

    parents_by_node: dict[str, set[str]] = {node_id: set() for node_id in states_by_node}
    for item in raw_edges:
        if not isinstance(item, Mapping):
            return False
        source = item.get("source")
        target = item.get("target")
        if (
            not isinstance(source, str)
            or not isinstance(target, str)
            or source not in states_by_node
            or target not in states_by_node
            or source == target
        ):
            return False
        parents_by_node[target].add(source)

    ancestors: set[str] = set()
    pending = list(failed_node_ids)
    while pending:
        node_id = pending.pop()
        for parent in parents_by_node[node_id]:
            if parent in ancestors:
                continue
            ancestors.add(parent)
            pending.append(parent)
    return all(states_by_node[node_id] == "SUCCEEDED" for node_id in ancestors)


class _RayExecutor(_Executor):
    def __init__(self, materialized_plan: Any | None = None) -> None:
        import ray

        from django_ray.runtime.context import get_current_task_context
        from django_ray.workflow.progress.limits import WORKFLOW_PROGRESS_LIMITS_V1

        self.ray = ray
        self.materialized_plan = materialized_plan

        remote_step, remote_collect, progress_actor_cls = _get_cached_workflow_remotes()
        self.remote_step = remote_step
        self.remote_collect = remote_collect

        self.task_context = get_current_task_context()
        self.task_execution_pk = (
            self.task_context.task_pk if self.task_context is not None else None
        )
        self.progress_actor = None
        self.workflow_run_identity: WorkflowRunIdentity | None = None
        self.last_progress_revision = -1
        self._last_progress_persisted_failed = False
        self.last_progress_flush_at = time.monotonic()
        self._pending_progress_snapshot_ref = None
        self._progress_suppression_depth = 0
        self._map_progress_sent_at: dict[str, float] = {}
        self._terminal_progress_publication_attempted = False
        self.reporting_policy = "full"
        self.workflow_progress_limits = WORKFLOW_PROGRESS_LIMITS_V1
        self.progress_actor_cls = progress_actor_cls
        if materialized_plan is not None:
            self.bind_plan(
                materialized_plan,
                requested_policy="auto",
                reporting_policy="full",
            )

    def bind_plan(
        self,
        materialized_plan: Any,
        *,
        requested_policy: str,
        reporting_policy: str = "full",
    ) -> None:
        from django_ray.workflow.plans import prepare_materialized_plan_for_ray

        super().bind_plan(
            materialized_plan,
            requested_policy=requested_policy,
            reporting_policy=reporting_policy,
        )
        self.reporting_policy = reporting_policy
        if self.task_context is None:
            self.materialized_plan = prepare_materialized_plan_for_ray(materialized_plan)
            return
        if (
            self.task_context.attempt_number is None
            or self.task_context.execution_generation is None
        ):
            self.materialized_plan = prepare_materialized_plan_for_ray(materialized_plan)
            return
        from django_ray.workflow.progress.runs import allocate_workflow_run

        selection = materialized_plan.plan.eligibility.select(
            "dynamic_tasks",
            requested_policy=requested_policy,
            reporting_policy=reporting_policy,
        )
        identity = allocate_workflow_run(
            self.task_context,
            plan=materialized_plan.plan,
            selection=selection,
        )
        if identity is None:
            from django_ray.workflow.plans import WorkflowPlanMismatchError

            raise WorkflowPlanMismatchError(
                "The durable task attempt is stale; workflow plan claim was rejected"
            )
        prepared_plan = prepare_materialized_plan_for_ray(materialized_plan)
        from django_ray.workflow.progress.runs import refresh_workflow_run_activity

        if not refresh_workflow_run_activity(identity):
            from django_ray.workflow.plans import WorkflowPlanMismatchError

            raise WorkflowPlanMismatchError(
                "The durable workflow run became stale during RuntimeEnv preparation"
            )
        self.workflow_run_identity = identity
        self.materialized_plan = prepared_plan
        if reporting_policy in {"disabled", "terminal_only"}:
            return
        from django_ray.conf.settings import get_settings
        from django_ray.workflow.progress.limits import (
            WORKFLOW_PROGRESS_LIMITS_V1,
            WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS,
        )
        from django_ray.workflow.progress.protocol import (
            WorkflowProgressEventKind,
            prepare_workflow_progress_event,
        )

        pilot_enabled = get_settings().get("WORKFLOW_PROGRESS_SCHEMA_V3_PILOT") is True
        progress_limits = (
            WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS
            if pilot_enabled
            else WORKFLOW_PROGRESS_LIMITS_V1
        )
        self.workflow_progress_limits = progress_limits

        initialized_event = prepare_workflow_progress_event(
            identity.as_dict(),
            WorkflowProgressEventKind.INITIALIZED,
            {"plan": materialized_plan.plan.summary()},
            limits=progress_limits,
        )
        self.progress_actor = (
            self.progress_actor_cls.remote(
                initialized_event,
                limits=progress_limits,
            )
            if pilot_enabled
            else self.progress_actor_cls.remote(initialized_event)
        )

    def _send_progress_event(
        self,
        actor: Any | None,
        kind: Any,
        payload: Mapping[str, Any],
    ) -> bool:
        """Best-effort one validated event while preserving the full run fence."""
        if actor is None:
            return False
        identity = self.workflow_run_identity
        if identity is None:
            raise AssertionError("a workflow progress actor requires a complete run identity")
        from django_ray.workflow.progress.protocol import send_workflow_progress_event

        try:
            send_workflow_progress_event(
                actor,
                identity.as_dict(),
                kind,
                payload,
                limits=self.workflow_progress_limits,
            )
        except BaseException:
            # Workflow observability remains best effort. The protocol prepares and
            # validates before the actor call, so invalid internal metadata cannot
            # cross Ray or interrupt application work.
            return False
        return True

    def _send_progress_edges(
        self,
        actor: Any | None,
        *,
        node_id: str,
        dependencies: Sequence[str],
    ) -> None:
        """Send dependency edges in independently bounded protocol batches."""
        if actor is None or not dependencies:
            return
        from django_ray.workflow.progress.protocol import WorkflowProgressEventKind

        edge_batch: list[dict[str, str]] = []
        for dependency in dependencies:
            edge_batch.append({"source": dependency, "target": node_id})
            if len(edge_batch) < self.workflow_progress_limits.edge_batch_max_items:
                continue
            self._send_progress_event(
                actor,
                WorkflowProgressEventKind.EDGES_REGISTERED,
                {"edges": edge_batch},
            )
            edge_batch = []
        if edge_batch:
            self._send_progress_event(
                actor,
                WorkflowProgressEventKind.EDGES_REGISTERED,
                {"edges": edge_batch},
            )

    def _strict_nested_request_kwargs(
        self,
        *,
        boundary_kind: Any,
        node_id: str,
        callable_path: str,
        binding: Any | None,
        output_preview_callable_path: str | None = None,
    ) -> dict[str, Any]:
        """Build one exact nested request only for an explicitly strict parent."""
        task_context = getattr(self, "task_context", None)
        if task_context is None or task_context.strict_execution_request is False:
            return {}

        from django_ray.execution_codec import (
            NestedCallableBindingKind,
            NestedExecutionRequest,
            NestedExecutionRequestRejected,
            NestedExecutionRequestRejection,
            NestedWorkflowBoundaryIdentity,
            encode_nested_execution_request,
            nested_runtime_env_digests,
        )
        from django_ray.runtime.context import (
            nested_execution_identity,
            require_strict_task_execution_context,
        )

        strict_context = require_strict_task_execution_context(task_context)
        outer_identity, execution_protocol_version = nested_execution_identity(strict_context)
        workflow_identity = self.workflow_run_identity
        if workflow_identity is None:
            raise NestedExecutionRequestRejected(
                NestedExecutionRequestRejection.MISSING_CONTEXT
            ) from None

        if binding is not None:
            runtime_env_plan_identity = _thaw_definition_value(binding.runtime_env_plan_identity)
        else:
            if getattr(self, "materialized_plan", None) is not None:
                raise NestedExecutionRequestRejected(
                    NestedExecutionRequestRejection.RUNTIME_ENV_MISMATCH
                ) from None
            runtime_env_plan_identity = deepcopy(strict_context.runtime_env_plan_identity)
        runtime_env_plan_digest, runtime_env_transport_digest = nested_runtime_env_digests(
            runtime_env_plan_identity
        )
        if binding is not None and (
            runtime_env_plan_digest != binding.runtime_env_plan_digest
            or runtime_env_transport_digest != binding.runtime_env_transport_digest
        ):
            raise NestedExecutionRequestRejected(
                NestedExecutionRequestRejection.RUNTIME_ENV_MISMATCH
            ) from None

        boundary_identity = NestedWorkflowBoundaryIdentity(
            workflow_run_id=workflow_identity.run_id,
            node_id=node_id,
        )
        serialized = encode_nested_execution_request(
            NestedExecutionRequest(
                outer_identity=outer_identity,
                execution_protocol_version=execution_protocol_version,
                boundary_kind=boundary_kind,
                boundary_identity=boundary_identity,
                callable_binding_kind=NestedCallableBindingKind.PATH,
                callable_binding=callable_path,
                output_preview_callable_path=output_preview_callable_path,
                runtime_env_plan_identity=runtime_env_plan_identity,
                runtime_env_plan_digest=runtime_env_plan_digest,
                runtime_env_transport_digest=runtime_env_transport_digest,
            )
        )
        return {
            "nested_execution_request": serialized,
            "expected_outer_task_execution_pk": outer_identity.task_execution_pk,
            "expected_outer_task_id": outer_identity.task_id,
            "expected_outer_attempt_number": outer_identity.attempt_number,
            "expected_outer_execution_generation": outer_identity.execution_generation,
            "expected_execution_protocol_version": execution_protocol_version,
            "expected_workflow_run_id": workflow_identity.run_id,
            "expected_node_id": node_id,
            "expected_runtime_env_plan_digest": runtime_env_plan_digest,
            "expected_runtime_env_transport_digest": runtime_env_transport_digest,
        }

    def submit_step(
        self,
        signature: Step,
        input_args: tuple[Any, ...],
        input_kwargs: dict[str, Any],
        node_id: str,
        dependencies: tuple[str, ...],
    ) -> Any:
        label = signature.callable_path.rsplit(".", 1)[-1]
        materialized_plan = getattr(self, "materialized_plan", None)
        binding = (
            materialized_plan.binding_for_node(node_id) if materialized_plan is not None else None
        )
        runtime_env_metadata: dict[str, Any] = {"mode": "inherit"}
        resolved_runtime_env = None
        if binding is not None:
            runtime_env_metadata = dict(binding.runtime_env_metadata)
            if binding.runtime_env_serialized is not None:
                from django_ray.runtime.runtime_env import normalize_runtime_env

                resolved_runtime_env = normalize_runtime_env(
                    json.loads(binding.runtime_env_serialized),
                    profile=binding.runtime_env_profile,
                    source=f"materialized workflow step {node_id} RuntimeEnv",
                )
        elif self.task_context is not None:
            runtime_env_metadata.update(
                {
                    "profile": self.task_context.runtime_env_profile,
                    "hash": self.task_context.runtime_env_hash,
                }
            )
        if binding is None and signature.runtime_env is not None:
            from django_ray.runtime.runtime_env import (
                normalize_runtime_env,
                prepare_runtime_env_for_ray_core,
                resolve_runtime_env_profile,
            )

            if isinstance(signature.runtime_env, str):
                resolved_runtime_env = resolve_runtime_env_profile(signature.runtime_env)
            else:
                resolved_runtime_env = normalize_runtime_env(
                    _thaw_definition_value(signature.runtime_env),
                    source=f"workflow step {label} RuntimeEnv",
                )
            runtime_env_metadata = {
                "mode": "override",
                "profile": resolved_runtime_env.profile,
                "hash": resolved_runtime_env.digest,
            }
        progress_actor = (
            self.progress_actor if getattr(self, "_progress_suppression_depth", 0) == 0 else None
        )
        if progress_actor is not None:
            plan_node = (
                materialized_plan.node_for_id(node_id) if materialized_plan is not None else None
            )
            from django_ray.workflow.progress.protocol import WorkflowProgressEventKind

            self._send_progress_event(
                progress_actor,
                WorkflowProgressEventKind.NODE_REGISTERED,
                {
                    "node_id": node_id,
                    "label": label,
                    "callable_path": signature.callable_path,
                    "runtime_env": runtime_env_metadata,
                    "ray_options": _json_safe(
                        plan_node["ray_options"] if plan_node is not None else {}
                    ),
                },
            )
            self._send_progress_edges(
                progress_actor,
                node_id=node_id,
                dependencies=dependencies,
            )
            if signature.output_preview_path is not None:
                from django_ray.workflow.previews import (
                    WorkflowOutputPreviewAvailability,
                    unavailable_workflow_output_preview,
                )

                self._send_progress_event(
                    progress_actor,
                    WorkflowProgressEventKind.OUTPUT_PREVIEW,
                    {
                        "node_id": node_id,
                        "output_preview": unavailable_workflow_output_preview(
                            WorkflowOutputPreviewAvailability.PENDING
                        ),
                    },
                )
        options = {
            "name": f"django_ray.workflow:{label}",
            **(
                binding.ray_options_dict()
                if binding is not None
                else _thaw_definition_value(signature.ray_options)
            ),
        }
        if resolved_runtime_env is not None:
            from django_ray.runtime.runtime_env import prepare_runtime_env_for_ray_core

            options["runtime_env"] = prepare_runtime_env_for_ray_core(resolved_runtime_env)
        remote_progress_kwargs: dict[str, Any] = {}
        if getattr(self, "reporting_policy", "full") != "terminal_only":
            remote_progress_kwargs["workflow_run_identity"] = (
                self.workflow_run_identity.as_dict()
                if self.workflow_run_identity is not None
                else None
            )
        from django_ray.workflow.progress.limits import WORKFLOW_PROGRESS_LIMITS_V1

        if (
            getattr(self, "reporting_policy", "full") != "terminal_only"
            and self.workflow_progress_limits != WORKFLOW_PROGRESS_LIMITS_V1
        ):
            remote_progress_kwargs["workflow_progress_limits"] = self.workflow_progress_limits
        output_preview_callable_path = (
            signature.output_preview_path if progress_actor is not None else None
        )
        if output_preview_callable_path is not None:
            remote_progress_kwargs["output_preview_path"] = output_preview_callable_path
        from django_ray.execution_codec import NestedExecutionBoundaryKind

        remote_progress_kwargs.update(
            self._strict_nested_request_kwargs(
                boundary_kind=NestedExecutionBoundaryKind.WORKFLOW_STEP,
                node_id=node_id,
                callable_path=signature.callable_path,
                output_preview_callable_path=output_preview_callable_path,
                binding=binding,
            )
        )
        object_ref = self.remote_step.options(**options).remote(
            signature.callable_path,
            signature.bootstrap_django,
            signature.bound_args,
            dict(signature.bound_kwargs),
            input_kwargs,
            self.task_execution_pk,
            progress_actor,
            node_id,
            *input_args,
            **remote_progress_kwargs,
        )
        if progress_actor is not None:
            try:
                ray_task_id = object_ref.task_id().hex()
            except (AttributeError, RuntimeError):
                pass
            else:
                self._send_progress_event(
                    progress_actor,
                    WorkflowProgressEventKind.SUBMITTED,
                    {
                        "node_id": node_id,
                        "label": label,
                        "ray_task_id": ray_task_id,
                    },
                )
        return object_ref

    def collect(self, values: list[Any]) -> Any:
        # Each value is a top-level argument so Ray resolves dependencies
        # before scheduling the collector.
        return self.remote_collect.remote(*values)

    def resolve(self, value: Any) -> Any:
        if self.progress_actor is None:
            return self.ray.get(value)

        from django_ray.conf.settings import get_settings

        flush_seconds = float(get_settings().get("WORKFLOW_PROGRESS_FLUSH_SECONDS", 1))
        while True:
            ready, _ = self.ray.wait([value], timeout=flush_seconds)
            self._flush_progress()
            if ready:
                return self.ray.get(value)
            if self.progress_actor is None:
                return self.ray.get(value)

    def _progress_warning(
        self,
        message: str,
        *,
        reason: str,
        **extra: Any,
    ) -> None:
        """Emit one bounded diagnostic without exposing workflow payloads."""
        from django_ray.logging import get_logger

        identity = self.workflow_run_identity
        get_logger(
            __name__,
            component="workflow_progress",
            task_execution_pk=(
                getattr(identity, "task_execution_pk", None)
                if identity is not None
                else getattr(self, "task_execution_pk", None)
            ),
            workflow_run_id=(getattr(identity, "run_id", None) if identity is not None else None),
        ).warning(
            message,
            extra={
                "reason": reason,
                **extra,
            },
        )

    def _disable_progress_reporting(
        self,
        *,
        notify_actor: bool = True,
        reason: str | None = None,
    ) -> None:
        """Stop local flushing and drain late reports in an obsolete actor."""
        progress_actor = self.progress_actor
        self.progress_actor = None
        self._pending_progress_snapshot_ref = None
        if reason is not None:
            self._progress_warning(
                "Workflow progress reporting became unavailable",
                reason=reason,
            )
        if not notify_actor or progress_actor is None:
            return
        try:
            progress_actor.disable.remote()
        except Exception:
            # Reporting is best effort and must not fail the workflow itself.
            return

    def store(self, value: Any) -> Any:
        return self.ray.put(value)

    def resolve_ready(self, value: Any) -> Any:
        return self.ray.get(value)

    def wait_one(self, values: Sequence[Any]) -> int:
        if self.progress_actor is None:
            ready, _ = self.ray.wait(list(values), num_returns=1)
            return values.index(ready[0])

        from django_ray.conf.settings import get_settings

        flush_seconds = float(get_settings().get("WORKFLOW_PROGRESS_FLUSH_SECONDS", 1))
        while True:
            ready, _ = self.ray.wait(
                list(values),
                num_returns=1,
                timeout=flush_seconds,
            )
            self._flush_progress()
            if ready:
                return values.index(ready[0])

    def _wait_result_buffer_refs(
        self,
        values: Sequence[Any],
        *,
        num_returns: int,
    ) -> list[Any]:
        """Poll readiness without fetching large objects into the coordinator."""
        from django_ray.conf.settings import get_settings

        flush_seconds = float(get_settings().get("WORKFLOW_PROGRESS_FLUSH_SECONDS", 1))
        while True:
            ready, _ = self.ray.wait(
                list(values),
                num_returns=num_returns,
                timeout=flush_seconds,
                fetch_local=False,
            )
            self._flush_progress()
            if len(ready) == num_returns:
                return ready

    def start_result_buffer(
        self,
        *,
        max_items: int,
        max_serialized_bytes: int,
        actor_options: Mapping[str, Any],
    ) -> _RayResultBufferSession:
        from django_ray.runtime.result_buffer import (
            result_buffer_ray_actor_options,
            validate_result_buffer_ack,
        )

        actor_cls = _get_cached_result_buffer_actor()
        actor = actor_cls.options(**result_buffer_ray_actor_options(actor_options)).remote(
            max_items, max_serialized_bytes
        )
        session = _RayResultBufferSession(actor=actor)
        ready_ref = None
        try:
            ready_ref = actor.ready.remote()
            validate_result_buffer_ack(self.resolve(ready_ref), state="ready")
        except BaseException:
            if ready_ref is not None:
                try:
                    self.ray.cancel(ready_ref, force=False, recursive=True)
                except BaseException:
                    pass
            self.discard_result_buffer(session, timeout_seconds=0)
            raise
        return session

    def wait_result_buffer_leaf(self, values: Sequence[Any]) -> int:
        ready = self._wait_result_buffer_refs(values, num_returns=1)
        return values.index(ready[0])

    def append_result_buffer(
        self,
        buffer: _RayResultBufferSession,
        *,
        index: int,
        value: Any,
    ) -> None:
        from django_ray.runtime.result_buffer import validate_result_buffer_ack

        ack_ref = buffer.actor.append.remote(index, value)
        try:
            ack = self.resolve(ack_ref)
            validate_result_buffer_ack(
                ack,
                state="retained",
                expected_index=index,
            )
        except BaseException:
            try:
                self.ray.cancel(ack_ref, force=False, recursive=True)
            except BaseException:
                pass
            raise

    def finalize_result_buffer(
        self,
        buffer: _RayResultBufferSession,
        *,
        expected_items: int,
    ) -> Any:
        from django_ray.runtime.result_buffer import validate_result_buffer_ack

        # Ray stores these as two direct return objects. The first is intentionally
        # never ray.get()'d here; only a downstream worker or the terminal caller
        # materializes the ordered Python value.
        payload_ref, ack_ref = buffer.actor.finalize.options(num_returns=2).remote(expected_items)
        try:
            self._wait_result_buffer_refs(
                [payload_ref, ack_ref],
                num_returns=2,
            )
            ack = self.resolve(ack_ref)
            validate_result_buffer_ack(
                ack,
                state="finalized",
                expected_items=expected_items,
            )
        except BaseException:
            for value in (payload_ref, ack_ref):
                try:
                    self.ray.cancel(value, force=False, recursive=True)
                except BaseException:
                    pass
            raise

        try:
            self.ray.kill(buffer.actor, no_restart=True)
        except BaseException:
            # Cleanup cannot replace a successfully materialized direct return.
            pass
        buffer.closed = True
        return payload_ref

    def discard_result_buffer(
        self,
        buffer: _RayResultBufferSession,
        *,
        timeout_seconds: float,
    ) -> None:
        if buffer.closed:
            return
        try:
            discard_ref = buffer.actor.discard.remote()
            ready, _ = self.ray.wait(
                [discard_ref],
                num_returns=1,
                timeout=timeout_seconds,
                fetch_local=False,
            )
            if ready:
                try:
                    self.ray.get(discard_ref)
                except BaseException:
                    pass
        except BaseException:
            pass
        finally:
            try:
                self.ray.kill(buffer.actor, no_restart=True)
            except BaseException:
                pass
            buffer.closed = True

    def _result_fold_runtime_env(self, reducer: Step, reducer_node_id: str) -> Any | None:
        materialized_plan = getattr(self, "materialized_plan", None)
        binding = (
            materialized_plan.binding_for_node(reducer_node_id)
            if materialized_plan is not None
            else None
        )
        if binding is not None:
            if binding.runtime_env_serialized is None:
                return None
            from django_ray.runtime.runtime_env import (
                normalize_runtime_env,
                prepare_runtime_env_for_ray_core,
            )

            resolved = normalize_runtime_env(
                json.loads(binding.runtime_env_serialized),
                profile=binding.runtime_env_profile,
                source=f"materialized workflow reducer {reducer_node_id} RuntimeEnv",
            )
            return prepare_runtime_env_for_ray_core(resolved)

        if reducer.runtime_env is None:
            return None
        from django_ray.runtime.runtime_env import (
            normalize_runtime_env,
            prepare_runtime_env_for_ray_core,
            resolve_runtime_env_profile,
        )

        resolved = (
            resolve_runtime_env_profile(reducer.runtime_env)
            if isinstance(reducer.runtime_env, str)
            else normalize_runtime_env(
                _thaw_definition_value(reducer.runtime_env),
                source=f"workflow reducer {reducer_node_id} RuntimeEnv",
            )
        )
        return prepare_runtime_env_for_ray_core(resolved)

    def start_result_fold(
        self,
        *,
        max_items: int,
        max_concurrency: int,
        max_serialized_bytes: int,
        actor_options: Mapping[str, Any],
        reducer: Step,
        reducer_node_id: str,
        initial: Any,
    ) -> _RayResultFoldSession:
        from django_ray.runtime.result_fold import (
            result_fold_ray_actor_options,
            validate_result_fold_ack,
        )

        ray_options = result_fold_ray_actor_options(actor_options)
        runtime_env = self._result_fold_runtime_env(reducer, reducer_node_id)
        if runtime_env is not None:
            ray_options["runtime_env"] = runtime_env
        materialized_plan = getattr(self, "materialized_plan", None)
        binding = (
            materialized_plan.binding_for_node(reducer_node_id)
            if materialized_plan is not None
            else None
        )
        from django_ray.execution_codec import NestedExecutionBoundaryKind

        nested_request_kwargs = self._strict_nested_request_kwargs(
            boundary_kind=NestedExecutionBoundaryKind.RESULT_FOLD,
            node_id=reducer_node_id,
            callable_path=reducer.callable_path,
            binding=binding,
        )
        actor_cls = _get_cached_result_fold_actor()
        actor = actor_cls.options(**ray_options).remote(
            max_items,
            max_concurrency,
            max_serialized_bytes,
            reducer.callable_path,
            reducer.bootstrap_django,
            reducer.bound_args,
            dict(reducer.bound_kwargs),
            initial,
            **nested_request_kwargs,
        )
        session = _RayResultFoldSession(
            actor=actor,
            maximum_items=max_items,
            maximum_concurrency=max_concurrency,
            maximum_out_of_order_items=min(max_items - 1, max_concurrency - 1),
            maximum_serialized_bytes=max_serialized_bytes,
        )
        ready_ref = None
        try:
            ready_ref = actor.ready.remote()
            ready = validate_result_fold_ack(self.resolve(ready_ref), state="ready")
            if (
                ready["folded_items"] != 0
                or ready["out_of_order_items"] != 0
                or ready["retained_bytes"] > max_serialized_bytes
            ):
                from django_ray.runtime.result_fold import ResultFoldProtocolError

                raise ResultFoldProtocolError(
                    "Result-fold ready acknowledgement reported invalid initial state"
                )
        except BaseException:
            if ready_ref is not None:
                try:
                    self.ray.cancel(ready_ref, force=False, recursive=True)
                except BaseException:
                    pass
            self.discard_result_fold(session, timeout_seconds=0)
            raise
        return session

    def wait_result_fold_leaf(self, values: Sequence[Any]) -> int:
        ready = self._wait_result_buffer_refs(values, num_returns=1)
        return values.index(ready[0])

    def append_result_fold(
        self,
        fold: _RayResultFoldSession,
        *,
        index: int,
        value: Any,
    ) -> int:
        from django_ray.runtime.result_fold import (
            ResultFoldProtocolError,
            validate_result_fold_ack,
        )

        ack_ref = fold.actor.append.remote(index, value)
        try:
            ack = self.resolve(ack_ref)
            validated = validate_result_fold_ack(
                ack,
                state="folded",
                expected_index=index,
            )
        except BaseException:
            try:
                self.ray.cancel(ack_ref, force=False, recursive=True)
            except BaseException:
                pass
            raise
        folded_items = int(validated["folded_items"])
        released_credits = int(validated["released_credits"])
        if released_credits != folded_items - fold.folded_items:
            raise ResultFoldProtocolError(
                "Result-fold acknowledgement released an invalid admission credit count"
            )
        if released_credits > fold.maximum_concurrency:
            raise ResultFoldProtocolError(
                "Result-fold acknowledgement released too many admission credits"
            )
        if folded_items > fold.maximum_items:
            raise ResultFoldProtocolError(
                "Result-fold acknowledgement exceeded the declared item bound"
            )
        if int(validated["out_of_order_items"]) > fold.maximum_out_of_order_items:
            raise ResultFoldProtocolError(
                "Result-fold acknowledgement exceeded the out-of-order retention bound"
            )
        if int(validated["retained_bytes"]) > fold.maximum_serialized_bytes:
            raise ResultFoldProtocolError(
                "Result-fold acknowledgement exceeded the serialized-byte bound"
            )
        fold.folded_items = folded_items
        return folded_items

    def cancel_and_drain_fold_payloads(
        self,
        values: Sequence[Any],
        *,
        timeout_seconds: float,
    ) -> None:
        for value in values:
            try:
                self.ray.cancel(value, force=False, recursive=True)
            except BaseException:
                pass
        if not values:
            return
        # Fold cleanup must not fetch or ray.get mapped payloads in the outer
        # coordinator, including error paths.
        self.ray.wait(
            list(values),
            num_returns=len(values),
            timeout=timeout_seconds,
            fetch_local=False,
        )

    def finalize_result_fold(
        self,
        fold: _RayResultFoldSession,
        *,
        expected_items: int,
    ) -> Any:
        from django_ray.runtime.result_fold import validate_result_fold_ack

        payload_ref, ack_ref = fold.actor.finalize.options(num_returns=2).remote(expected_items)
        try:
            self._wait_result_buffer_refs(
                [payload_ref, ack_ref],
                num_returns=2,
            )
            ack = self.resolve(ack_ref)
            validated = validate_result_fold_ack(
                ack,
                state="finalized",
                expected_items=expected_items,
            )
            if int(validated["retained_bytes"]) > fold.maximum_serialized_bytes:
                from django_ray.runtime.result_fold import ResultFoldProtocolError

                raise ResultFoldProtocolError(
                    "Result-fold final acknowledgement exceeded the serialized-byte bound"
                )
        except BaseException:
            for value in (payload_ref, ack_ref):
                try:
                    self.ray.cancel(value, force=False, recursive=True)
                except BaseException:
                    pass
            raise

        try:
            self.ray.kill(fold.actor, no_restart=True)
        except BaseException:
            pass
        fold.closed = True
        return payload_ref

    def discard_result_fold(
        self,
        fold: _RayResultFoldSession,
        *,
        timeout_seconds: float,
    ) -> None:
        if fold.closed:
            return
        try:
            discard_ref = fold.actor.discard.remote()
            ready, _ = self.ray.wait(
                [discard_ref],
                num_returns=1,
                timeout=timeout_seconds,
                fetch_local=False,
            )
            if ready:
                try:
                    self.ray.get(discard_ref)
                except BaseException:
                    pass
        except BaseException:
            pass
        finally:
            try:
                self.ray.kill(fold.actor, no_restart=True)
            except BaseException:
                pass
            fold.closed = True

    def cancel_and_drain(
        self,
        values: Sequence[Any],
        *,
        timeout_seconds: float,
    ) -> None:
        for value in values:
            try:
                self.ray.cancel(value, force=False, recursive=True)
            except BaseException:
                # Cleanup is deliberately subordinate to the original map error.
                pass
        if not values:
            return
        ready, _ = self.ray.wait(
            list(values),
            num_returns=len(values),
            timeout=timeout_seconds,
        )
        for value in ready:
            try:
                self.ray.get(value)
            except BaseException:
                pass

    @contextmanager
    def suppress_progress(self) -> Iterator[None]:
        self._progress_suppression_depth += 1
        try:
            yield
        finally:
            self._progress_suppression_depth -= 1

    def map_started(
        self,
        node_id: str,
        label: str,
        dependencies: tuple[str, ...],
        *,
        max_concurrency: int | None,
        max_items: int | None,
    ) -> None:
        if self.progress_actor is None or self._progress_suppression_depth:
            return
        from django_ray.workflow.progress.protocol import WorkflowProgressEventKind

        sent = self._send_progress_event(
            self.progress_actor,
            WorkflowProgressEventKind.MAP_REGISTERED,
            {
                "node_id": node_id,
                "label": label,
                "max_concurrency": max_concurrency,
                "max_items": max_items,
            },
        )
        self._send_progress_edges(
            self.progress_actor,
            node_id=node_id,
            dependencies=dependencies,
        )
        if sent:
            self._map_progress_sent_at[node_id] = time.monotonic()

    def map_progress(
        self,
        node_id: str,
        label: str,
        *,
        submitted: int,
        completed: int,
        input_exhausted: bool,
        force: bool = False,
    ) -> None:
        if self.progress_actor is None or self._progress_suppression_depth:
            return

        from django_ray.conf.settings import get_settings

        now = time.monotonic()
        flush_seconds = float(get_settings().get("WORKFLOW_PROGRESS_FLUSH_SECONDS", 1))
        last_sent = self._map_progress_sent_at.get(node_id, 0.0)
        if not force and now - last_sent < flush_seconds:
            return
        from django_ray.workflow.progress.protocol import WorkflowProgressEventKind

        if self._send_progress_event(
            self.progress_actor,
            WorkflowProgressEventKind.MAP_PROGRESS,
            {
                "node_id": node_id,
                "label": label,
                "submitted": submitted,
                "completed": completed,
                "input_exhausted": input_exhausted,
            },
        ):
            self._map_progress_sent_at[node_id] = now

    def map_finished(
        self,
        node_id: str,
        label: str,
        *,
        submitted: int,
        completed: int,
        input_exhausted: bool,
        failed: bool = False,
        error: str | None = None,
    ) -> None:
        if self.progress_actor is None or self._progress_suppression_depth:
            return
        from django_ray.workflow.progress.protocol import WorkflowProgressEventKind

        self._send_progress_event(
            self.progress_actor,
            WorkflowProgressEventKind.MAP_PROGRESS,
            {
                "node_id": node_id,
                "label": label,
                "submitted": submitted,
                "completed": completed,
                "input_exhausted": input_exhausted,
            },
        )
        self._send_progress_event(
            self.progress_actor,
            (WorkflowProgressEventKind.FAILED if failed else WorkflowProgressEventKind.COMPLETED),
            (
                {
                    "node_id": node_id,
                    "label": label,
                    "error": error or "Map failed",
                }
                if failed
                else {
                    "node_id": node_id,
                    "label": label,
                }
            ),
        )
        self._map_progress_sent_at.pop(node_id, None)

    def _flush_progress(
        self,
        *,
        bypass_interval: bool = False,
        failed: bool = False,
        wait_timeout_seconds: float = 0.5,
    ) -> dict[str, Any] | None:
        if self.progress_actor is None or self.workflow_run_identity is None:
            return None

        from django_ray.conf.settings import get_settings

        now = time.monotonic()
        flush_seconds = float(get_settings().get("WORKFLOW_PROGRESS_FLUSH_SECONDS", 1))
        last_flush_at = getattr(self, "last_progress_flush_at", 0.0)
        if not bypass_interval and now - last_flush_at < flush_seconds:
            return None
        self.last_progress_flush_at = now

        # Avoid blocking the caller indefinitely if the snapshot actor is unhealthy
        # or transiently unavailable under load.
        snapshot_ref = getattr(self, "_pending_progress_snapshot_ref", None)
        try:
            if snapshot_ref is None:
                snapshot_ref = self.progress_actor.snapshot.remote()
                self._pending_progress_snapshot_ref = snapshot_ref
            ready, _ = self.ray.wait(
                [snapshot_ref],
                timeout=wait_timeout_seconds,
            )
        except Exception:
            self._disable_progress_reporting(
                notify_actor=False,
                reason="snapshot_rpc_failed",
            )
            return None
        if not ready:
            return None
        self._pending_progress_snapshot_ref = None

        try:
            snapshot = self.ray.get(ready[0])
        except Exception:
            # If the actor died (e.g. OOM), disable further tracking attempts
            # so we don't crash the workflow or repeatedly timeout.
            self._disable_progress_reporting(
                notify_actor=False,
                reason="snapshot_get_failed",
            )
            return None

        if failed:
            snapshot["state"] = "FAILED"
        revision = int(snapshot["revision"])
        already_persisted_failed = getattr(
            self,
            "_last_progress_persisted_failed",
            False,
        )
        if revision != self.last_progress_revision or (failed and not already_persisted_failed):
            from django_ray.workflow.progress.runs import persist_workflow_progress

            try:
                accepted = persist_workflow_progress(
                    self.workflow_run_identity,
                    snapshot,
                )
            except Exception:
                self._disable_progress_reporting(
                    reason="snapshot_persistence_failed",
                )
                return None
            if not accepted:
                self._disable_progress_reporting(
                    reason="snapshot_fence_rejected",
                )
                return None
            self.last_progress_revision = revision
            self._last_progress_persisted_failed = failed
        return snapshot

    def finish_progress(self, *, failed: bool = False) -> None:
        if getattr(self, "reporting_policy", "full") == "terminal_only":
            return
        if self.progress_actor is None:
            return

        from django_ray.conf.settings import get_settings

        config = get_settings()
        timeout_seconds = float(
            config.get(
                "WORKFLOW_PROGRESS_TERMINAL_FLUSH_TIMEOUT_SECONDS",
                15,
            )
        )
        schema_v3_pilot_enabled = config.get("WORKFLOW_PROGRESS_SCHEMA_V3_PILOT") is True
        deadline = time.monotonic() + timeout_seconds
        saw_snapshot = False

        # Leaf event reporting is asynchronous. Give the in-memory actor a brief
        # chance to drain its mailbox before writing the terminal snapshot. A
        # newly scheduled actor can also be transiently unavailable, so keep
        # polling one pending snapshot request until the explicit total deadline.
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            snapshot = self._flush_progress(
                bypass_interval=True,
                failed=failed,
                wait_timeout_seconds=min(0.5, remaining),
            )
            if self.progress_actor is None:
                return
            if snapshot is not None:
                saw_snapshot = True
                terminal = snapshot["completed_nodes"] + snapshot["failed_nodes"]
                ingress = snapshot.get("ingress")
                ingress_cannot_publish = (
                    schema_v3_pilot_enabled
                    and isinstance(ingress, Mapping)
                    and bool(ingress.get("rejected") or ingress.get("truncated"))
                )
                failure_evidence_ready = failed and (
                    not schema_v3_pilot_enabled
                    or _failed_snapshot_has_causally_complete_ancestors(snapshot)
                )
                if (
                    ingress_cannot_publish
                    or failure_evidence_ready
                    or (not failed and terminal == snapshot["total_nodes"])
                ):
                    self._publish_terminal_progress(snapshot)
                    return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.05, remaining))

        reason = "snapshot_incomplete" if saw_snapshot else "snapshot_unavailable"
        self._progress_warning(
            "Workflow terminal progress did not complete before the flush deadline",
            reason=reason,
            timeout_seconds=timeout_seconds,
            failed_workflow=failed,
        )
        self._disable_progress_reporting()

    def _publish_terminal_progress(self, snapshot: dict[str, Any]) -> bool:
        """Best-effort one default-off schema-v3 terminal publication."""
        if self.workflow_run_identity is None or getattr(
            self,
            "_terminal_progress_publication_attempted",
            False,
        ):
            return False

        from django_ray.conf.settings import get_settings

        config = get_settings()
        if config.get("WORKFLOW_PROGRESS_SCHEMA_V3_PILOT") is not True:
            return False
        self._terminal_progress_publication_attempted = True
        try:
            from django_ray.workflow.progress.publication import (
                publish_terminal_workflow_progress,
            )

            publication = publish_terminal_workflow_progress(
                self.workflow_run_identity,
                snapshot,
                detail_days=int(config.get("WORKFLOW_PROGRESS_DETAIL_RETENTION_DAYS", 7)),
            )
        except BaseException:
            self._progress_warning(
                "Workflow schema-v3 pilot publication was not completed",
                reason="publication_failed",
            )
            return False
        if not publication.accepted:
            self._progress_warning(
                "Workflow schema-v3 pilot publication was not completed",
                reason=publication.reason.value,
            )
            return False
        return True


class WorkflowSignature(ABC):
    """A lazy, reusable workflow expression."""

    @property
    def _workflow_definition_kind(self) -> WorkflowDefinitionKind | None:
        """Return the compiler kind for built-in definitions."""
        return None

    @abstractmethod
    def _submit(
        self,
        executor: _Executor,
        input_args: tuple[Any, ...],
        input_kwargs: dict[str, Any],
        node_id: str,
        dependencies: tuple[str, ...],
    ) -> _Submission:
        """Submit this expression and return its value or future."""

    def run(
        self,
        *args: Any,
        use_ray: bool | None = None,
        **kwargs: Any,
    ) -> Any:
        """Execute the workflow and return its final concrete result.

        Ray is used automatically when it is installed and initialized.
        ``use_ray=False`` provides a deterministic local fallback for sync
        workers and tests. ``use_ray=True`` requires an initialized Ray client.
        """
        return self._run_with_configuration(
            args,
            kwargs,
            use_ray=use_ray,
            reporting_policy=None,
        )

    def with_progress_reporting(self, policy: str) -> _ConfiguredWorkflowRun:
        """Return an invocation runner with an explicit progress policy."""
        return _ConfiguredWorkflowRun(
            signature=self,
            reporting_policy=_workflow_progress_policy(policy),
        )

    def _run_with_configuration(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        use_ray: bool | None,
        reporting_policy: str | None,
    ) -> Any:
        """Execute with package-owned options separate from application kwargs."""
        from django_ray.runtime.context import get_current_task_context
        from django_ray.workflow.plans import materialize_workflow_plan

        reporting_policy = _workflow_progress_policy(reporting_policy)
        materialized_plan = materialize_workflow_plan(
            self,
            invocation_args=args,
            invocation_kwargs=kwargs,
            task_context=get_current_task_context(),
        )
        executor = _get_executor(use_ray)
        executor.bind_plan(
            materialized_plan,
            requested_policy=(
                "local" if use_ray is False else ("dynamic_tasks" if use_ray is True else "auto")
            ),
            reporting_policy=(
                "disabled" if isinstance(executor, _LocalExecutor) else reporting_policy
            ),
        )
        try:
            submission = self._submit(executor, args, kwargs, "0", ())
            result = executor.resolve(submission.value)
        except BaseException:
            executor.finish_progress(failed=True)
            raise
        executor.finish_progress()
        return result


@dataclass(frozen=True)
class _ConfiguredWorkflowRun:
    """One explicit execution configuration without reserving task keywords."""

    signature: WorkflowSignature
    reporting_policy: str

    def run(
        self,
        *args: Any,
        use_ray: bool | None = None,
        **kwargs: Any,
    ) -> Any:
        """Execute the underlying signature with this reporting policy."""
        return self.signature._run_with_configuration(
            args,
            kwargs,
            use_ray=use_ray,
            reporting_policy=self.reporting_policy,
        )


@dataclass(frozen=True)
class Step(WorkflowSignature):
    """One importable callable submitted as a lightweight Ray task."""

    callable_path: str
    bound_args: tuple[Any, ...] = ()
    bound_kwargs: Mapping[str, Any] = field(default_factory=dict)
    bootstrap_django: bool = False
    ray_options: Mapping[str, Any] = field(default_factory=dict)
    runtime_env: str | Mapping[str, Any] | None = None
    output_preview_path: str | None = None

    @property
    def _workflow_definition_kind(self) -> WorkflowDefinitionKind:
        return WorkflowDefinitionKind.STEP

    def __post_init__(self) -> None:
        # Bound values are invocation data, not plan metadata. Freeze only the
        # outer keyword map so nested application dictionaries, lists, tuples,
        # and custom objects retain their original execution types.
        object.__setattr__(self, "bound_kwargs", _FrozenMapping(self.bound_kwargs))
        object.__setattr__(self, "ray_options", _freeze_definition_value(self.ray_options))
        if isinstance(self.runtime_env, Mapping):
            object.__setattr__(self, "runtime_env", _freeze_definition_value(self.runtime_env))
        if self.output_preview_path is not None:
            object.__setattr__(
                self,
                "output_preview_path",
                _callable_path(self.output_preview_path),
            )

    def _submit(
        self,
        executor: _Executor,
        input_args: tuple[Any, ...],
        input_kwargs: dict[str, Any],
        node_id: str,
        dependencies: tuple[str, ...],
    ) -> _Submission:
        value = executor.submit_step(
            self,
            input_args,
            input_kwargs,
            node_id,
            dependencies,
        )
        executor.track_cleanup(value)
        return _Submission(
            value=value,
            terminal_node_ids=(node_id,),
        )

    def with_options(self, **ray_options: Any) -> Step:
        """Return a copy with additional Ray task options."""
        return Step(
            callable_path=self.callable_path,
            bound_args=self.bound_args,
            bound_kwargs=dict(self.bound_kwargs),
            bootstrap_django=self.bootstrap_django,
            ray_options={**_thaw_definition_value(self.ray_options), **ray_options},
            runtime_env=_clone_runtime_env(self.runtime_env),
            output_preview_path=self.output_preview_path,
        )

    def with_runtime_env(self, runtime_env: str | dict[str, Any] | None) -> Step:
        """Return a copy using a named profile or inline RuntimeEnv."""
        return Step(
            callable_path=self.callable_path,
            bound_args=self.bound_args,
            bound_kwargs=dict(self.bound_kwargs),
            bootstrap_django=self.bootstrap_django,
            ray_options=dict(self.ray_options),
            runtime_env=_clone_runtime_env(runtime_env),
            output_preview_path=self.output_preview_path,
        )

    def with_output_preview(self, projector: Callable[[Any], Any] | str | None) -> Step:
        """Return a copy with one explicit author-owned diagnostic projection."""
        output_preview_path = None if projector is None else _callable_path(projector)
        return Step(
            callable_path=self.callable_path,
            bound_args=self.bound_args,
            bound_kwargs=dict(self.bound_kwargs),
            bootstrap_django=self.bootstrap_django,
            ray_options=dict(self.ray_options),
            runtime_env=_clone_runtime_env(self.runtime_env),
            output_preview_path=output_preview_path,
        )


@dataclass(frozen=True)
class Chain(WorkflowSignature):
    """Run signatures sequentially, passing each result to the next."""

    signatures: tuple[WorkflowSignature, ...]

    @property
    def _workflow_definition_kind(self) -> WorkflowDefinitionKind:
        return WorkflowDefinitionKind.CHAIN

    def _submit(
        self,
        executor: _Executor,
        input_args: tuple[Any, ...],
        input_kwargs: dict[str, Any],
        node_id: str,
        dependencies: tuple[str, ...],
    ) -> _Submission:
        result: _Submission | None = None
        for index, signature in enumerate(self.signatures):
            if index == 0:
                result = signature._submit(
                    executor,
                    input_args,
                    input_kwargs,
                    f"{node_id}.{index}",
                    dependencies,
                )
            else:
                assert result is not None
                result = signature._submit(
                    executor,
                    (result.value,),
                    {},
                    f"{node_id}.{index}",
                    result.terminal_node_ids,
                )
        assert result is not None
        return result


@dataclass(frozen=True)
class Group(WorkflowSignature):
    """Fan out the same input to several signatures and gather their results."""

    signatures: tuple[WorkflowSignature, ...]

    @property
    def _workflow_definition_kind(self) -> WorkflowDefinitionKind:
        return WorkflowDefinitionKind.GROUP

    def _submit(
        self,
        executor: _Executor,
        input_args: tuple[Any, ...],
        input_kwargs: dict[str, Any],
        node_id: str,
        dependencies: tuple[str, ...],
    ) -> _Submission:
        results = [
            signature._submit(
                executor,
                input_args,
                input_kwargs,
                f"{node_id}.g{index}",
                dependencies,
            )
            for index, signature in enumerate(self.signatures)
        ]
        value = executor.collect([result.value for result in results])
        executor.track_cleanup(value)
        return _Submission(
            value=value,
            terminal_node_ids=tuple(
                node for result in results for node in result.terminal_node_ids
            ),
        )


@dataclass(frozen=True)
class _MapResultBuffer:
    """Immutable public-builder selection for the v1 Ray result buffer."""

    max_serialized_bytes: int
    actor_options: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "actor_options", _freeze_definition_value(self.actor_options))


@dataclass(frozen=True)
class _MapResultFold:
    """Immutable public-builder selection for the v1 ordered result fold."""

    reducer: Step
    initial: Any
    max_serialized_bytes: int
    actor_options: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "actor_options", _freeze_definition_value(self.actor_options))


@dataclass(frozen=True)
class Map(WorkflowSignature):
    """Fan out one signature over an iterable produced by an earlier stage."""

    signature: WorkflowSignature
    max_concurrency: int | None = None
    max_items: int | None = None
    cancel_timeout_seconds: float = 1.0
    result_buffer: _MapResultBuffer | None = None
    result_fold: _MapResultFold | None = None

    @property
    def _workflow_definition_kind(self) -> WorkflowDefinitionKind:
        return WorkflowDefinitionKind.MAP

    def __post_init__(self) -> None:
        _validate_map_limit("max_concurrency", self.max_concurrency, minimum=1)
        _validate_map_limit("max_items", self.max_items, minimum=1)
        _validate_map_cancel_timeout(self.cancel_timeout_seconds)
        if self.result_buffer is not None and self.result_fold is not None:
            raise ValueError("Result-buffer and result-fold modes are mutually exclusive")
        if (self.result_buffer is not None or self.result_fold is not None) and (
            self.max_concurrency is None or self.max_items is None
        ):
            raise ValueError(
                "Actor-backed map results require positive max_concurrency and max_items limits"
            )

    def with_limits(
        self,
        *,
        max_concurrency: int | None = None,
        max_items: int | None = None,
        cancel_timeout_seconds: float = 1.0,
    ) -> Map:
        """Return a bounded copy with an admission window and/or item cap."""
        if max_concurrency is None and max_items is None:
            raise ValueError("with_limits requires max_concurrency or max_items")
        return Map(
            self.signature,
            max_concurrency=max_concurrency,
            max_items=max_items,
            cancel_timeout_seconds=cancel_timeout_seconds,
            result_buffer=self.result_buffer,
            result_fold=self.result_fold,
        )

    def with_result_buffer(
        self,
        *,
        max_serialized_bytes: int,
        actor_options: Mapping[str, Any],
    ) -> Map:
        """Keep bounded Ray map results in a resource-accounted actor."""
        if self.result_fold is not None:
            raise ValueError("Result-buffer and result-fold modes are mutually exclusive")
        if self.max_concurrency is None or self.max_items is None:
            raise ValueError(
                "with_result_buffer requires positive max_concurrency and max_items limits"
            )
        if isinstance(max_serialized_bytes, bool) or not isinstance(max_serialized_bytes, int):
            raise TypeError("max_serialized_bytes must be an integer")
        if max_serialized_bytes < 1:
            raise ValueError("max_serialized_bytes must be at least 1")
        from django_ray.runtime.result_buffer import normalize_result_buffer_actor_options

        normalized_options = normalize_result_buffer_actor_options(
            actor_options,
            max_serialized_bytes=max_serialized_bytes,
        )
        return Map(
            self.signature,
            max_concurrency=self.max_concurrency,
            max_items=self.max_items,
            cancel_timeout_seconds=self.cancel_timeout_seconds,
            result_buffer=_MapResultBuffer(
                max_serialized_bytes=max_serialized_bytes,
                actor_options=normalized_options,
            ),
            result_fold=None,
        )

    def reduce(
        self,
        reducer: Step,
        *,
        initial: Any,
        max_serialized_bytes: int,
        actor_options: Mapping[str, Any],
    ) -> Map:
        """Fold bounded map results in strict input order inside one Ray actor."""
        if self.result_buffer is not None:
            raise ValueError("Result-buffer and result-fold modes are mutually exclusive")
        if self.result_fold is not None:
            raise ValueError("A result fold is already configured for this map")
        if self.max_concurrency is None or self.max_items is None:
            raise ValueError("reduce requires positive max_concurrency and max_items limits")
        if not isinstance(reducer, Step):
            raise TypeError("reduce requires one Step reducer")
        if reducer.ray_options:
            fields = ", ".join(sorted(str(field) for field in reducer.ray_options))
            raise WorkflowDefinitionError(
                "reduce does not support reducer Ray task options; configure actor_options "
                f"instead (unsupported: {fields})"
            )
        import inspect

        try:
            reducer_callable = import_callable(reducer.callable_path)
        except (ImportError, AttributeError):
            # A reducer may be supplied only by its effective RuntimeEnv. The
            # actor repeats this validation after that environment is installed
            # and before its readiness acknowledgement admits mapper effects.
            pass
        else:
            if (
                inspect.iscoroutinefunction(reducer_callable)
                or inspect.isgeneratorfunction(reducer_callable)
                or inspect.isasyncgenfunction(reducer_callable)
            ):
                raise WorkflowDefinitionError(
                    "reduce requires a synchronous non-generator reducer callable"
                )
        if isinstance(max_serialized_bytes, bool) or not isinstance(max_serialized_bytes, int):
            raise TypeError("max_serialized_bytes must be an integer")
        if max_serialized_bytes < 1:
            raise ValueError("max_serialized_bytes must be at least 1")
        from django_ray.runtime.result_fold import normalize_result_fold_actor_options

        normalized_options = normalize_result_fold_actor_options(
            actor_options,
            max_serialized_bytes=max_serialized_bytes,
        )
        return Map(
            self.signature,
            max_concurrency=self.max_concurrency,
            max_items=self.max_items,
            cancel_timeout_seconds=self.cancel_timeout_seconds,
            result_buffer=None,
            result_fold=_MapResultFold(
                reducer=reducer,
                initial=initial,
                max_serialized_bytes=max_serialized_bytes,
                actor_options=normalized_options,
            ),
        )

    def _submit(
        self,
        executor: _Executor,
        input_args: tuple[Any, ...],
        input_kwargs: dict[str, Any],
        node_id: str,
        dependencies: tuple[str, ...],
    ) -> _Submission:
        if len(input_args) != 1 or input_kwargs:
            raise WorkflowDefinitionError("map_step expects exactly one iterable input")

        items = executor.resolve(input_args[0])
        if isinstance(items, (str, bytes, dict)) or not isinstance(items, Iterable):
            raise WorkflowDefinitionError("map_step input must be a non-string iterable")

        if self.max_items is not None and isinstance(items, Sized):
            if len(items) > self.max_items:
                raise WorkflowDefinitionError(f"map_step input exceeds max_items={self.max_items}")

        if self.max_concurrency is not None or self.max_items is not None:
            return self._submit_bounded(
                executor,
                items,
                node_id,
                dependencies,
            )

        results = [
            self.signature._submit(
                executor,
                (item,),
                {},
                f"{node_id}.m{index}",
                dependencies,
            )
            for index, item in enumerate(items)
        ]
        value = executor.collect([result.value for result in results])
        executor.track_cleanup(value)
        return _Submission(
            value=value,
            terminal_node_ids=(
                tuple(node for result in results for node in result.terminal_node_ids)
                if results
                else dependencies
            ),
        )

    def _submit_bounded(
        self,
        executor: _Executor,
        items: Iterable[Any],
        node_id: str,
        dependencies: tuple[str, ...],
    ) -> _Submission:
        """Execute a map with bounded admission and incremental collection."""
        iterator = iter(items)
        label = f"map:{_signature_label(self.signature)}"
        pending: list[tuple[int, _Submission, tuple[Any, ...]]] = []
        ordered_results: list[Any] | None = None
        fold_accumulator = None
        fold_ready_results: dict[int, Any] = {}
        submitted = 0
        completed = 0
        input_exhausted = False
        resolving_cleanup: tuple[Any, ...] = ()
        admitting_cleanup: list[Any] | None = None
        result_buffer = None
        result_fold = None
        executor.map_started(
            node_id,
            label,
            dependencies,
            max_concurrency=self.max_concurrency,
            max_items=self.max_items,
        )

        try:
            if self.result_buffer is not None:
                assert self.max_items is not None
                result_buffer = executor.start_result_buffer(
                    max_items=self.max_items,
                    max_serialized_bytes=self.result_buffer.max_serialized_bytes,
                    actor_options=_thaw_definition_value(self.result_buffer.actor_options),
                )
            elif self.result_fold is not None:
                assert self.max_items is not None
                assert self.max_concurrency is not None
                result_fold = executor.start_result_fold(
                    max_items=self.max_items,
                    max_concurrency=self.max_concurrency,
                    max_serialized_bytes=self.result_fold.max_serialized_bytes,
                    actor_options=_thaw_definition_value(self.result_fold.actor_options),
                    reducer=self.result_fold.reducer,
                    reducer_node_id=f"{node_id}.reducer",
                    initial=self.result_fold.initial,
                )
                if result_fold is None:
                    from django_ray.runtime.result_fold import clone_result_fold_initial

                    # Local mode deliberately skips the Ray retained-byte limit,
                    # but validates and clones the invocation value before leaves.
                    fold_accumulator = clone_result_fold_initial(self.result_fold.initial)
            if result_buffer is None and self.result_fold is None:
                ordered_results = []

            while pending or not input_exhausted:
                while not input_exhausted and (
                    (
                        self.result_fold is not None
                        and self.max_concurrency is not None
                        and submitted - completed < self.max_concurrency
                    )
                    or (
                        self.result_fold is None
                        and (self.max_concurrency is None or len(pending) < self.max_concurrency)
                    )
                ):
                    try:
                        item = next(iterator)
                    except StopIteration:
                        input_exhausted = True
                        executor.map_progress(
                            node_id,
                            label,
                            submitted=submitted,
                            completed=completed,
                            input_exhausted=True,
                            force=True,
                        )
                        break

                    if self.max_items is not None and submitted >= self.max_items:
                        raise WorkflowDefinitionError(
                            f"map_step input exceeds max_items={self.max_items}"
                        )

                    admitting_cleanup = []
                    with (
                        executor.suppress_progress(),
                        executor.capture_cleanup(admitting_cleanup),
                    ):
                        result = self.signature._submit(
                            executor,
                            (item,),
                            {},
                            f"{node_id}.m{submitted}",
                            dependencies,
                        )
                    if ordered_results is not None:
                        ordered_results.append(None)
                    pending.append((submitted, result, tuple(reversed(admitting_cleanup))))
                    admitting_cleanup = None
                    submitted += 1
                    executor.map_progress(
                        node_id,
                        label,
                        submitted=submitted,
                        completed=completed,
                        input_exhausted=False,
                    )

                if not pending:
                    continue

                pending_values = [result.value for _, result, _ in pending]
                ready_index = (
                    executor.wait_result_fold_leaf(pending_values)
                    if result_fold is not None
                    else (
                        executor.wait_result_buffer_leaf(pending_values)
                        if result_buffer is not None
                        else executor.wait_one(pending_values)
                    )
                )
                result_index, result, resolving_cleanup = pending.pop(ready_index)
                if result_fold is not None:
                    folded_items = executor.append_result_fold(
                        result_fold,
                        index=result_index,
                        value=result.value,
                    )
                    if folded_items < completed or folded_items > submitted:
                        from django_ray.runtime.result_fold import ResultFoldProtocolError

                        raise ResultFoldProtocolError(
                            "Result-fold acknowledgement reported an invalid incorporated count"
                        )
                    completed = folded_items
                elif self.result_fold is not None:
                    fold_ready_results[result_index] = executor.resolve_ready(result.value)
                    while completed in fold_ready_results:
                        fold_accumulator = executor.reduce_local(
                            self.result_fold.reducer,
                            fold_accumulator,
                            fold_ready_results.pop(completed),
                        )
                        completed += 1
                elif result_buffer is not None:
                    executor.append_result_buffer(
                        result_buffer,
                        index=result_index,
                        value=result.value,
                    )
                    completed += 1
                else:
                    assert ordered_results is not None
                    ordered_results[result_index] = executor.resolve_ready(result.value)
                    completed += 1
                resolving_cleanup = ()
                executor.map_progress(
                    node_id,
                    label,
                    submitted=submitted,
                    completed=completed,
                    input_exhausted=input_exhausted,
                )

            if result_fold is not None:
                value = executor.finalize_result_fold(
                    result_fold,
                    expected_items=submitted,
                )
            elif self.result_fold is not None:
                if completed != submitted or fold_ready_results:
                    from django_ray.runtime.result_fold import ResultFoldProtocolError

                    raise ResultFoldProtocolError(
                        "Local result-fold finalization found missing or unexpected item indices"
                    )
                value = executor.store(fold_accumulator)
            elif result_buffer is not None:
                value = executor.finalize_result_buffer(
                    result_buffer,
                    expected_items=submitted,
                )
            else:
                assert ordered_results is not None
                value = executor.store(ordered_results)
        except BaseException as error:
            nested_rejection = None
            if isinstance(error, Exception):
                from django_ray.execution_codec import (
                    find_nested_execution_request_rejection,
                )

                nested_rejection = find_nested_execution_request_rejection(error)
            _close_iterator(iterator)
            cleanup_values = list(reversed(admitting_cleanup or ()))
            cleanup_values.extend(resolving_cleanup)
            cleanup_values.extend(cleanup for _, _, values in pending for cleanup in values)
            try:
                if self.result_fold is not None:
                    executor.cancel_and_drain_fold_payloads(
                        cleanup_values,
                        timeout_seconds=self.cancel_timeout_seconds,
                    )
                else:
                    executor.cancel_and_drain(
                        cleanup_values,
                        timeout_seconds=self.cancel_timeout_seconds,
                    )
            except BaseException:
                pass
            if result_buffer is not None:
                try:
                    executor.discard_result_buffer(
                        result_buffer,
                        timeout_seconds=self.cancel_timeout_seconds,
                    )
                except BaseException:
                    pass
            if result_fold is not None:
                try:
                    executor.discard_result_fold(
                        result_fold,
                        timeout_seconds=self.cancel_timeout_seconds,
                    )
                except BaseException:
                    pass
            try:
                executor.map_finished(
                    node_id,
                    label,
                    submitted=submitted,
                    completed=completed,
                    input_exhausted=input_exhausted,
                    failed=True,
                    error=str(nested_rejection or error),
                )
            except BaseException:
                pass
            if nested_rejection is not None:
                raise nested_rejection from None
            raise

        executor.map_finished(
            node_id,
            label,
            submitted=submitted,
            completed=completed,
            input_exhausted=True,
        )
        return _Submission(
            value=value,
            terminal_node_ids=(node_id,),
        )


def _validate_map_limit(name: str, value: int | None, *, minimum: int) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer or None")
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")


def _validate_map_cancel_timeout(value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("cancel_timeout_seconds must be a number")
    if not math.isfinite(value) or value < 0:
        raise ValueError("cancel_timeout_seconds must be finite and non-negative")


def _signature_label(signature: WorkflowSignature) -> str:
    if isinstance(signature, Step):
        return signature.callable_path.rsplit(".", 1)[-1]
    return type(signature).__name__.lower()


def _close_iterator(iterator: Iterator[Any]) -> None:
    close = getattr(iterator, "close", None)
    if close is None:
        return
    try:
        close()
    except BaseException:
        # Iterator cleanup cannot replace the original workflow failure.
        pass


def step(
    callable_obj: Callable[..., Any] | str,
    *bound_args: Any,
    django: bool = False,
    ray_options: dict[str, Any] | None = None,
    runtime_env: str | dict[str, Any] | None = None,
    **bound_kwargs: Any,
) -> Step:
    """Create a lightweight workflow step signature."""
    if ray_options and "runtime_env" in ray_options:
        if runtime_env is not None:
            raise WorkflowDefinitionError(
                "Set runtime_env directly, not in both runtime_env and ray_options"
            )
        runtime_env = ray_options["runtime_env"]
        ray_options = {key: value for key, value in ray_options.items() if key != "runtime_env"}
    return Step(
        callable_path=_callable_path(callable_obj),
        bound_args=bound_args,
        bound_kwargs=bound_kwargs,
        bootstrap_django=django,
        ray_options={} if ray_options is None else dict(ray_options),
        runtime_env=_clone_runtime_env(runtime_env),
    )


def chain(*signatures: WorkflowSignature) -> Chain:
    """Create a sequential workflow."""
    if not signatures:
        raise WorkflowDefinitionError("chain requires at least one signature")
    return Chain(tuple(signatures))


def group(*signatures: WorkflowSignature) -> Group:
    """Create a static fan-out workflow."""
    if not signatures:
        raise WorkflowDefinitionError("group requires at least one signature")
    return Group(tuple(signatures))


def map_step(
    callable_or_signature: Callable[..., Any] | str | WorkflowSignature,
    *bound_args: Any,
    django: bool = False,
    ray_options: dict[str, Any] | None = None,
    runtime_env: str | dict[str, Any] | None = None,
    **bound_kwargs: Any,
) -> Map:
    """Create a dynamic fan-out over the preceding iterable."""
    if isinstance(callable_or_signature, WorkflowSignature):
        if bound_args or bound_kwargs or django or ray_options or runtime_env is not None:
            raise WorkflowDefinitionError(
                "Options and bound arguments cannot be added to an existing signature"
            )
        signature = callable_or_signature
    else:
        signature = step(
            callable_or_signature,
            *bound_args,
            django=django,
            ray_options=ray_options,
            runtime_env=runtime_env,
            **bound_kwargs,
        )
    return Map(signature)


def _get_executor(
    use_ray: bool | None,
    *,
    materialized_plan: Any | None = None,
) -> _Executor:
    try:
        import ray

        ray_ready = ray.is_initialized()
    except ImportError:
        ray_ready = False

    if use_ray is True and not ray_ready:
        from django_ray.runtime.context import get_current_task_context

        task_context = get_current_task_context()
        if task_context is not None and task_context.ray_job_driver:
            ray.init(address="auto", ignore_reinit_error=True)
            ray_ready = True

    if use_ray is True and not ray_ready:
        raise RuntimeError("use_ray=True requires Ray to be installed and initialized")
    if use_ray is not False and ray_ready:
        return _RayExecutor() if materialized_plan is None else _RayExecutor(materialized_plan)
    return _LocalExecutor() if materialized_plan is None else _LocalExecutor(materialized_plan)


def _workflow_progress_policy(value: str | None) -> str:
    """Resolve and validate one invocation's effective reporting policy."""
    from django.core.exceptions import ImproperlyConfigured

    from django_ray.conf.defaults import (
        DEFAULTS,
        WORKFLOW_PROGRESS_RUNTIME_REPORTING_POLICIES,
    )
    from django_ray.conf.settings import get_settings

    if value is not None:
        policy = value
    else:
        try:
            policy = get_settings()["WORKFLOW_PROGRESS_REPORTING_POLICY"]
        except ImproperlyConfigured:
            policy = DEFAULTS["WORKFLOW_PROGRESS_REPORTING_POLICY"]
    if not isinstance(policy, str) or policy not in WORKFLOW_PROGRESS_RUNTIME_REPORTING_POLICIES:
        valid_policies = ", ".join(sorted(WORKFLOW_PROGRESS_RUNTIME_REPORTING_POLICIES))
        raise WorkflowDefinitionError(
            f"Workflow progress reporting policy must be one of: {valid_policies}"
        )
    return policy


__all__ = [
    "Chain",
    "Group",
    "Map",
    "Step",
    "WorkflowDefinitionError",
    "WorkflowSignature",
    "chain",
    "group",
    "map_step",
    "report_progress",
    "step",
]
