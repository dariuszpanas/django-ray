"""Whole-plan execution-strategy seam (#70) and default-off Compiled Graph strategy (#72).

This module provides:
- The explicit whole-plan execution-strategy contract for plan validation,
  preparation, admission, execution, result consumption, cancellation, drain,
  cleanup, and bounded observability metadata (available even when node-level
  progress reporting is disabled).
- Strategy-specific opaque handles so dynamic Ray ``ObjectRef`` assumptions do
  not leak into static-actor or Compiled Graph execution.
- Explicit region representation (`WorkflowRegionSpec`) at the plan boundary
  while V1 rejects mixed dynamic/static partition execution before submission.
- Strict pre-start versus post-start fallback rules and run-identity callback
  fencing so retries or fallbacks never duplicate side effects.
- The minimum per-run fixed-actor builder (`build_fixed_actor_graph_plan`) and
  the explicit default-off `CompiledGraphExecutionStrategy` guarded behind
  `DJANGO_RAY["COMPILED_GRAPH_EXPERIMENTAL_ENABLED"]` and `#86`/`#159`
  fail-closed capability checks.
"""

from __future__ import annotations

import inspect
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from django.core.exceptions import ImproperlyConfigured

from django_ray.runtime.compiled_graph import (
    CompiledGraphCapabilityDecision,
    CompiledGraphRuntimeIdentity,
    CompiledGraphSubmissionTransport,
    CompiledGraphTopology,
    CompiledGraphTransport,
    detect_compiled_graph_runtime,
    evaluate_compiled_graph_support,
)
from django_ray.runtime.compiled_graph_lifecycle import (
    COMPILED_GRAPH_LIFECYCLE_PROTOCOL_VERSION,
    CompiledGraphInvocationState,
    CompiledGraphLifecycleAdapter,
    CompiledGraphLifecycleState,
    CompiledGraphSessionState,
    LifecycleAction,
    LifecycleActionKind,
    LifecycleCapacity,
    LifecycleDeadlines,
    LifecycleEffectState,
    LifecycleEvent,
    LifecycleEventKind,
    LifecycleOutcome,
    LifecycleRejection,
    LifecycleRetryDisposition,
    initial_session,
    lifecycle_snapshot,
    reduce_lifecycle,
)
from django_ray.runtime.context import (
    WorkflowInvocationIdentity,
    WorkflowRunIdentity,
    get_current_task_context,
)
from django_ray.runtime.import_utils import import_callable
from django_ray.workflow.contracts import WorkflowDefinitionKind
from django_ray.workflow.plans import (
    MAX_REJECTIONS,
    MaterializedWorkflowPlan,
    PlanEligibility,
    PlanRejection,
    PlanSelection,
    WorkflowPlanValidationError,
    validate_plan_selection_manifest,
)

if TYPE_CHECKING:
    from django_ray.workflows import WorkflowSignature


class WorkflowExecutionStrategyName(StrEnum):
    """Supported internal whole-plan execution strategies."""

    LOCAL = "local"
    DYNAMIC_TASKS = "dynamic_tasks"
    STATIC_ACTORS = "static_actors"
    COMPILED_GRAPH = "compiled_graph"


class WorkflowRequestedStrategyPolicy(StrEnum):
    """Caller-requested strategy selection modes."""

    AUTO = "auto"
    LOCAL = "local"
    DYNAMIC_TASKS = "dynamic_tasks"
    STATIC_ACTORS = "static_actors"
    COMPILED_GRAPH = "compiled_graph"


class WorkflowStrategySelectionError(WorkflowPlanValidationError):
    """Raised before submission when a requested strategy is ineligible or disabled."""

    def __init__(
        self,
        message: str,
        *,
        requested_policy: str,
        rejections: tuple[PlanRejection, ...] = (),
    ) -> None:
        super().__init__(message)
        self.requested_policy = requested_policy
        self.rejections = rejections


class WorkflowStrategyFallbackError(RuntimeError):
    """Raised when a caller or adapter attempts a prohibited strategy fallback."""


class StaleWorkflowRunCallbackError(RuntimeError):
    """Raised when a stale run/invocation callback targets a newer attempt."""


@dataclass(frozen=True)
class WorkflowRegionSpec:
    """Explicit caller-declared workflow region for future mixed-strategy plans (#96).

    V1 represents and validates explicit regions at the plan/strategy boundary
    without inferring partitions or executing mixed dynamic/static graphs.
    """

    region_id: str
    node_ids: tuple[str, ...]
    requested_strategy: str

    def __post_init__(self) -> None:
        if not isinstance(self.region_id, str) or not self.region_id.strip():
            raise WorkflowPlanValidationError("WorkflowRegionSpec.region_id must be non-empty")
        if (
            not isinstance(self.node_ids, tuple)
            or not self.node_ids
            or any(not isinstance(node_id, str) or not node_id for node_id in self.node_ids)
        ):
            raise WorkflowPlanValidationError(
                "WorkflowRegionSpec.node_ids must be a non-empty tuple of node ids"
            )
        if self.requested_strategy not in {item.value for item in WorkflowExecutionStrategyName}:
            raise WorkflowPlanValidationError(
                f"WorkflowRegionSpec.requested_strategy {self.requested_strategy!r} is unsupported"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "region_id": self.region_id,
            "node_ids": list(self.node_ids),
            "requested_strategy": self.requested_strategy,
        }


@dataclass(frozen=True)
class WorkflowStrategyObservability:
    """Bounded strategy selection and lifecycle metadata independent of node progress."""

    plan_fingerprint: str
    definition_name: str
    definition_revision: str
    requested_policy: str
    selected_strategy: str
    reporting_policy: str
    eligible_strategies: tuple[str, ...]
    rejections: tuple[PlanRejection, ...]
    total_rejections: int
    regions: tuple[WorkflowRegionSpec, ...] = ()
    fallback_allowed_before_start: bool = False
    effect_state: LifecycleEffectState = LifecycleEffectState.NOT_STARTED
    retry_disposition: LifecycleRetryDisposition = LifecycleRetryDisposition.AUTOMATIC_ALLOWED
    lifecycle_snapshot: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        selection_dict = PlanSelection(
            requested_policy=self.requested_policy,
            selected_strategy=self.selected_strategy,
            eligible_strategies=self.eligible_strategies,
            rejections=self.rejections,
            total_rejections=self.total_rejections,
            reporting_policy=self.reporting_policy,
        ).as_dict()
        validate_plan_selection_manifest(selection_dict)
        payload: dict[str, Any] = {
            "plan_fingerprint": self.plan_fingerprint,
            "definition_name": self.definition_name,
            "definition_revision": self.definition_revision,
            "plan_selection": selection_dict,
            "regions": [region.as_dict() for region in self.regions],
            "fallback_allowed_before_start": self.fallback_allowed_before_start,
            "effect_state": self.effect_state.value,
            "retry_disposition": self.retry_disposition.value,
        }
        if self.lifecycle_snapshot is not None:
            payload["lifecycle_snapshot"] = dict(self.lifecycle_snapshot)
        return payload


class WorkflowStrategyHandle(Protocol):
    """Strategy-specific opaque execution handle (never a raw Ray ObjectRef)."""

    @property
    def strategy_name(self) -> str:
        """Return the concrete strategy that owns this handle."""
        ...

    @property
    def plan_fingerprint(self) -> str:
        """Return the canonical fingerprint of the materialized plan."""
        ...

    @property
    def run_identity(self) -> WorkflowRunIdentity | None:
        """Return the owning workflow run identity, if bound."""
        ...

    @property
    def invocation_id(self) -> str:
        """Return the unique invocation identifier within the session."""
        ...

    @property
    def opaque_token(self) -> str:
        """Return a stable non-persistable opaque handle token."""
        ...


@dataclass
class LocalStrategyHandle:
    """Opaque handle owned by ``LocalWorkflowExecutionStrategy``."""

    plan_fingerprint: str
    run_identity: WorkflowRunIdentity | None
    invocation_id: str
    opaque_token: str
    _result: Any = field(default=None, repr=False)
    _consumed: bool = False
    _cancelled: bool = False
    _drained: bool = False
    _cleaned_up: bool = False

    @property
    def strategy_name(self) -> str:
        return WorkflowExecutionStrategyName.LOCAL.value


@dataclass
class DynamicTasksStrategyHandle:
    """Opaque handle owned by ``DynamicTasksWorkflowExecutionStrategy``."""

    plan_fingerprint: str
    run_identity: WorkflowRunIdentity | None
    invocation_id: str
    opaque_token: str
    _executor: Any = field(default=None, repr=False)
    _submission: Any = field(default=None, repr=False)
    _resolved_result: Any = field(default=None, repr=False)
    _resolved: bool = False
    _consumed: bool = False
    _cancelled: bool = False
    _drained: bool = False
    _cleaned_up: bool = False

    @property
    def strategy_name(self) -> str:
        return WorkflowExecutionStrategyName.DYNAMIC_TASKS.value


@dataclass
class CompiledGraphStrategyHandle:
    """Opaque handle owned by ``CompiledGraphExecutionStrategy``."""

    plan_fingerprint: str
    run_identity: WorkflowRunIdentity
    invocation_identity: WorkflowInvocationIdentity
    opaque_token: str
    fixed_actor_plan: FixedActorGraphPlan
    lifecycle_state: CompiledGraphLifecycleState
    _adapter: InProcessFixedActorAdapter | CompiledGraphLifecycleAdapter = field(repr=False)
    _output_value: Any = field(default=None, repr=False)
    _output_ready: bool = False
    _consumed: bool = False
    _cancelled: bool = False
    _drained: bool = False
    _cleaned_up: bool = False

    @property
    def strategy_name(self) -> str:
        return WorkflowExecutionStrategyName.COMPILED_GRAPH.value

    @property
    def invocation_id(self) -> str:
        return self.invocation_identity.invocation_id


@dataclass(frozen=True)
class PreparedWorkflowStrategy:
    """Validated and prepared strategy binding prior to invocation admission."""

    strategy_name: str
    materialized_plan: MaterializedWorkflowPlan
    selection: PlanSelection
    run_identity: WorkflowRunIdentity | None
    regions: tuple[WorkflowRegionSpec, ...] = ()
    fixed_actor_plan: FixedActorGraphPlan | None = None
    lifecycle_state: CompiledGraphLifecycleState | None = None
    effect_state: LifecycleEffectState = LifecycleEffectState.NOT_STARTED


@dataclass(frozen=True)
class AdmittedWorkflowInvocation:
    """Single admitted workflow invocation ready for strategy execution."""

    prepared: PreparedWorkflowStrategy
    invocation_identity: WorkflowInvocationIdentity
    lifecycle_state: CompiledGraphLifecycleState | None = None


def verify_run_callback_identity(
    expected: WorkflowRunIdentity | None,
    observed: WorkflowRunIdentity | None,
) -> None:
    """Fence callbacks and cleanup against stale workflow attempts/generations (#81)."""
    if expected is None:
        return
    if observed is None or observed != expected:
        raise StaleWorkflowRunCallbackError(
            "Stale workflow run callback rejected: callback identity does not match "
            "the active workflow run identity"
        )


def attempt_strategy_fallback(
    *,
    requested_policy: str,
    from_strategy: str,
    to_strategy: str,
    effect_state: LifecycleEffectState,
    eligibility: PlanEligibility,
    reporting_policy: str | None = None,
) -> PlanSelection:
    """Enforce strict pre-start vs post-start strategy fallback semantics (#70).

    - Post-start fallback (``effect_state == MAY_HAVE_STARTED``) is unconditionally
      prohibited to prevent duplicate side effects.
    - Explicit strategy requests never silently fall back to another strategy.
    - Pre-start fallback is allowed only for ``requested_policy == "auto"`` before
      any preparation or submission side effects have started, and never targets
      ``compiled_graph``.
    """
    if effect_state is not LifecycleEffectState.NOT_STARTED:
        raise WorkflowStrategyFallbackError(
            f"Cannot fall back from {from_strategy!r} to {to_strategy!r} after "
            "execution or preparation side effects may have started"
        )
    if requested_policy != WorkflowRequestedStrategyPolicy.AUTO.value:
        raise WorkflowStrategyFallbackError(
            f"Explicit strategy policy {requested_policy!r} prohibits fallback "
            f"from {from_strategy!r} to {to_strategy!r}"
        )
    if to_strategy == WorkflowExecutionStrategyName.COMPILED_GRAPH.value:
        raise WorkflowStrategyFallbackError(
            "Automatic strategy fallback may never select 'compiled_graph'"
        )
    return eligibility.select(
        to_strategy,
        requested_policy=requested_policy,
        reporting_policy=reporting_policy,
    )


def compiled_graph_experimental_enabled(config: Mapping[str, Any] | None = None) -> bool:
    """Return whether the global experimental Compiled Graph gate is enabled (#72)."""
    if config is None:
        from django.conf import settings as django_settings

        from django_ray.conf.defaults import DEFAULTS
        from django_ray.conf.settings import get_settings

        config = get_settings() if django_settings.configured else dict(DEFAULTS)
    value = config.get("COMPILED_GRAPH_EXPERIMENTAL_ENABLED", False)
    if not isinstance(value, bool):
        raise ImproperlyConfigured(
            "django-ray: COMPILED_GRAPH_EXPERIMENTAL_ENABLED must be a boolean"
        )
    return value


@dataclass(frozen=True)
class StaticActorPoolSpec:
    """Fixed-width actor pool configuration for a static stage (#71/#72)."""

    replicas: int = 1

    def __post_init__(self) -> None:
        if (
            isinstance(self.replicas, bool)
            or not isinstance(self.replicas, int)
            or self.replicas < 1
        ):
            raise WorkflowPlanValidationError(
                "StaticActorPoolSpec.replicas must be an integer >= 1"
            )
        if self.replicas > 64:
            raise WorkflowPlanValidationError(
                "StaticActorPoolSpec.replicas must not exceed 64 in V1"
            )


@dataclass(frozen=True)
class StaticActorStageSpec:
    """One dedicated fixed actor stage materialized by the minimum builder (#71/#72)."""

    stage_id: str
    logical_node_ids: tuple[str, ...]
    callable_path: str
    replicas: int
    dependencies: tuple[str, ...]
    resources: Mapping[str, Any]
    scheduling: Mapping[str, Any]
    runtime_env_digest: str
    runtime_env_profile: str | None
    bound_args: tuple[Any, ...] = ()
    bound_kwargs: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "logical_node_ids": list(self.logical_node_ids),
            "callable_path": self.callable_path,
            "replicas": self.replicas,
            "dependencies": list(self.dependencies),
            "resources": dict(self.resources),
            "scheduling": dict(self.scheduling),
            "runtime_env_digest": self.runtime_env_digest,
            "runtime_env_profile": self.runtime_env_profile,
        }


@dataclass(frozen=True)
class FixedActorGraphPlan:
    """Minimum fixed-actor graph representation for static plans (#71/#72)."""

    plan_fingerprint: str
    topology_kind: str
    stages: tuple[StaticActorStageSpec, ...]
    terminal_stage_ids: tuple[str, ...]
    gather_terminal_stages: bool
    trust_identity: Mapping[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_fingerprint": self.plan_fingerprint,
            "topology_kind": self.topology_kind,
            "stages": [stage.as_dict() for stage in self.stages],
            "terminal_stage_ids": list(self.terminal_stage_ids),
            "gather_terminal_stages": self.gather_terminal_stages,
            "trust_identity": dict(self.trust_identity),
        }


def build_fixed_actor_graph_plan(
    signature: WorkflowSignature,
    materialized_plan: MaterializedWorkflowPlan,
    *,
    pool_specs: Mapping[str, StaticActorPoolSpec] | None = None,
) -> tuple[FixedActorGraphPlan | None, tuple[PlanRejection, ...]]:
    """Build the minimum per-run fixed-actor plan or return bounded rejections (#71/#72).

    Supports fixed `Step`, `Chain`, `Group` (scatter/gather), and fixed-width
    worker-pool stage replicas while keeping logical node ids distinct from
    physical actor stages.
    """
    rejections: list[PlanRejection] = []
    manifest = materialized_plan.plan.manifest

    if "snapshot" in manifest:
        rejections.append(
            PlanRejection(
                "compiled_graph",
                "PLAN_SNAPSHOT_OVERFLOW",
                "snapshot",
                "effective plan exceeded bounded snapshot limits",
            )
        )
    if manifest["topology"]["class"] != "static":
        rejections.append(
            PlanRejection(
                "compiled_graph",
                "DYNAMIC_TOPOLOGY",
                "topology.class",
                "fixed-actor and Compiled Graph execution require static workflow topology",
            )
        )

    for existing in materialized_plan.plan.eligibility.rejections:
        if existing.strategy == "compiled_graph" and (
            existing.code
            in {
                "UNRESOLVED_CODE_IDENTITY",
                "UNRESOLVED_PLAN_OPTION",
                "UNRESOLVED_RUNTIME_ENV",
                "UNBOUNDED_ADMISSION",
            }
            or (
                existing.code == "INCOMPATIBLE_PLATFORM"
                and existing.path == "compatibility.compiled_graph.runtime.deployment_profile"
            )
        ):
            rejections.append(existing)

    stages: list[StaticActorStageSpec] = []
    pools = dict(pool_specs or {})

    def _visit(
        current: WorkflowSignature,
        node_id: str,
        upstream_stage_ids: tuple[str, ...],
    ) -> tuple[tuple[str, ...], bool]:
        kind = current._workflow_definition_kind
        if kind is WorkflowDefinitionKind.MAP:
            rejections.append(
                PlanRejection(
                    "compiled_graph",
                    "DYNAMIC_TOPOLOGY",
                    f"nodes.{node_id}",
                    "map_step uses runtime-dependent fan-out and must run on dynamic_tasks",
                )
            )
            return (), False
        if kind is WorkflowDefinitionKind.STEP:
            from django_ray.workflows import Step

            assert isinstance(current, Step)
            if current.bootstrap_django:
                rejections.append(
                    PlanRejection(
                        "compiled_graph",
                        "DJANGO_ORM_UNSUPPORTED",
                        f"nodes.{node_id}.bootstrap_django",
                        "Django/ORM bootstrap is unsupported inside fixed-actor Compiled Graph stages",
                    )
                )
            try:
                callable_obj = import_callable(current.callable_path)
            except Exception:
                rejections.append(
                    PlanRejection(
                        "compiled_graph",
                        "UNRESOLVED_CODE_IDENTITY",
                        f"nodes.{node_id}.callable",
                        f"Callable {current.callable_path!r} could not be imported at preparation time",
                    )
                )
            else:
                if (
                    inspect.iscoroutinefunction(callable_obj)
                    or inspect.isgeneratorfunction(callable_obj)
                    or inspect.isasyncgenfunction(callable_obj)
                    or not (inspect.isfunction(callable_obj) or inspect.isbuiltin(callable_obj))
                ):
                    rejections.append(
                        PlanRejection(
                            "compiled_graph",
                            "UNSUPPORTED_CALLABLE_KIND",
                            f"nodes.{node_id}.callable",
                            "fixed-actor stages require synchronous module-level functions",
                        )
                    )
            node_manifest = materialized_plan.node_for_id(node_id)
            binding = materialized_plan.binding_for_node(node_id)
            if node_manifest is None or binding is None:
                rejections.append(
                    PlanRejection(
                        "compiled_graph",
                        "MISSING_NODE_BINDING",
                        f"nodes.{node_id}",
                        "materialized plan is missing node execution binding",
                    )
                )
                return (), False
            pool = pools.get(node_id, StaticActorPoolSpec(replicas=1))
            stage_id = f"actor_stage:{len(stages)}"
            stages.append(
                StaticActorStageSpec(
                    stage_id=stage_id,
                    logical_node_ids=(node_id,),
                    callable_path=current.callable_path,
                    replicas=pool.replicas,
                    dependencies=upstream_stage_ids,
                    resources=MappingProxyType(dict(node_manifest.get("resources", {}))),
                    scheduling=MappingProxyType(dict(node_manifest.get("scheduling", {}))),
                    runtime_env_digest=str(
                        binding.runtime_env_plan_digest
                        or manifest["environments"]["outer"]["digest"]
                    ),
                    runtime_env_profile=binding.runtime_env_profile,
                    bound_args=tuple(current.bound_args),
                    bound_kwargs=MappingProxyType(dict(current.bound_kwargs)),
                )
            )
            return (stage_id,), False
        if kind is WorkflowDefinitionKind.CHAIN:
            from django_ray.workflows import Chain

            assert isinstance(current, Chain)
            current_upstream = upstream_stage_ids
            gather = False
            for index, child in enumerate(current.signatures):
                current_upstream, gather = _visit(
                    child,
                    f"{node_id}.{index}",
                    current_upstream,
                )
            return current_upstream, gather
        if kind is WorkflowDefinitionKind.GROUP:
            from django_ray.workflows import Group

            assert isinstance(current, Group)
            branch_terminals: list[str] = []
            for index, child in enumerate(current.signatures):
                child_terminals, child_gather = _visit(
                    child,
                    f"{node_id}.g{index}",
                    upstream_stage_ids,
                )
                if child_gather or len(child_terminals) != 1:
                    rejections.append(
                        PlanRejection(
                            "compiled_graph",
                            "NESTED_GROUP_GATHER_UNSUPPORTED",
                            f"nodes.{node_id}.g{index}",
                            "nested scatter/gather groups without an intervening stage are unsupported in V1",
                        )
                    )
                branch_terminals.extend(child_terminals)
            return tuple(branch_terminals), True
        rejections.append(
            PlanRejection(
                "compiled_graph",
                "UNSUPPORTED_NODE_MODEL",
                f"nodes.{node_id}",
                "workflow signature kind is not supported by the fixed-actor builder",
            )
        )
        return (), False

    terminal_stage_ids, gather_terminals = _visit(signature, "0", ())
    if rejections:
        deduped = sorted(
            {(r.strategy, r.code, r.path, r.message): r for r in rejections}.values(),
            key=lambda item: (item.strategy, item.code, item.path),
        )
        return None, tuple(deduped[:MAX_REJECTIONS])

    topology_kind = (
        "worker_pool"
        if any(stage.replicas > 1 for stage in stages)
        else (
            "scatter_gather"
            if gather_terminals or any(len(stage.dependencies) > 1 for stage in stages)
            else ("chain" if len(stages) > 1 else "single_stage")
        )
    )
    trust_identity = manifest.get("security", {}).get("trust_identity", {})
    return (
        FixedActorGraphPlan(
            plan_fingerprint=materialized_plan.plan.fingerprint,
            topology_kind=topology_kind,
            stages=tuple(stages),
            terminal_stage_ids=terminal_stage_ids,
            gather_terminal_stages=gather_terminals,
            trust_identity=MappingProxyType(dict(trust_identity)),
        ),
        (),
    )


class _IsolatedStageActorReplica:
    """Per-run dedicated stage replica that clears state on drain/teardown (#71/#72)."""

    def __init__(self, spec: StaticActorStageSpec, replica_index: int) -> None:
        self.spec = spec
        self.replica_index = replica_index
        self._callable = import_callable(spec.callable_path)
        self.invocation_context: dict[str, Any] = {}
        self.cached_inputs: list[Any] = []
        self.cached_outputs: list[Any] = []
        self.invocations: int = 0
        self.cleaned_up: bool = False

    def invoke(self, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> Any:
        if self.cleaned_up:
            raise RuntimeError(f"Actor stage {self.spec.stage_id} replica has been torn down")
        self.invocations += 1
        call_args = (*self.spec.bound_args, *args)
        call_kwargs = {**kwargs, **dict(self.spec.bound_kwargs)}
        self.cached_inputs.append((call_args, call_kwargs))
        try:
            result = self._callable(*call_args, **call_kwargs)
            self.cached_outputs.append(result)
            return result
        finally:
            self.invocation_context.clear()

    def clear_state(self) -> None:
        self.invocation_context.clear()
        self.cached_inputs.clear()
        self.cached_outputs.clear()
        self.cleaned_up = True


class InProcessFixedActorAdapter(CompiledGraphLifecycleAdapter):
    """Hermetic process-local adapter executing `FixedActorGraphPlan` through `#85` lifecycle."""

    def __init__(
        self,
        fixed_plan: FixedActorGraphPlan,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.fixed_plan = fixed_plan
        self._clock = clock
        self.replicas_by_stage: dict[str, tuple[_IsolatedStageActorReplica, ...]] = {}
        self._rr_cursor: dict[str, int] = {}
        self._pending_args: tuple[Any, ...] = ()
        self._pending_kwargs: dict[str, Any] = {}
        self._buffered_output: Any = None
        self._has_buffered_output: bool = False
        self.prepared: bool = False
        self.drained: bool = False
        self.torn_down: bool = False

    def bind_invocation_inputs(
        self,
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> None:
        self._pending_args = tuple(args)
        self._pending_kwargs = dict(kwargs)

    def pop_consumed_output(self) -> Any:
        value = self._buffered_output
        self._buffered_output = None
        self._has_buffered_output = False
        return value

    def dispatch(self, action: LifecycleAction) -> LifecycleEvent:
        now = float(self._clock())
        if action.kind is LifecycleActionKind.VALIDATE:
            return LifecycleEvent(
                kind=LifecycleEventKind.VALIDATED,
                session_identity=action.session_identity,
                observed_at=now,
                action_token=action.token,
            )
        if action.kind is LifecycleActionKind.PREPARE:
            try:
                for stage in self.fixed_plan.stages:
                    self.replicas_by_stage[stage.stage_id] = tuple(
                        _IsolatedStageActorReplica(stage, index) for index in range(stage.replicas)
                    )
                    self._rr_cursor[stage.stage_id] = 0
                self.prepared = True
            except Exception:
                return LifecycleEvent(
                    kind=LifecycleEventKind.PREPARATION_FAILED,
                    session_identity=action.session_identity,
                    observed_at=now,
                    action_token=action.token,
                )
            return LifecycleEvent(
                kind=LifecycleEventKind.PREPARED,
                session_identity=action.session_identity,
                observed_at=now,
                action_token=action.token,
            )
        if action.kind is LifecycleActionKind.ADMIT:
            if self._has_buffered_output:
                return LifecycleEvent(
                    kind=LifecycleEventKind.CAPACITY_REJECTED,
                    session_identity=action.session_identity,
                    invocation_identity=action.invocation_identity,
                    observed_at=now,
                    action_token=action.token,
                )
            return LifecycleEvent(
                kind=LifecycleEventKind.ADMITTED,
                session_identity=action.session_identity,
                invocation_identity=action.invocation_identity,
                observed_at=now,
                action_token=action.token,
            )
        if action.kind is LifecycleActionKind.SUBMIT:
            stage_outputs: dict[str, Any] = {}
            for stage in self.fixed_plan.stages:
                replicas = self.replicas_by_stage[stage.stage_id]
                cursor = self._rr_cursor[stage.stage_id] % len(replicas)
                self._rr_cursor[stage.stage_id] = cursor + 1
                replica = replicas[cursor]
                if not stage.dependencies:
                    stage_args = self._pending_args
                    stage_kwargs = self._pending_kwargs
                elif len(stage.dependencies) == 1:
                    stage_args = (stage_outputs[stage.dependencies[0]],)
                    stage_kwargs = {}
                else:
                    stage_args = ([stage_outputs[dep] for dep in stage.dependencies],)
                    stage_kwargs = {}
                stage_outputs[stage.stage_id] = replica.invoke(stage_args, stage_kwargs)
            if self.fixed_plan.gather_terminal_stages:
                self._buffered_output = [
                    stage_outputs[stage_id] for stage_id in self.fixed_plan.terminal_stage_ids
                ]
            else:
                terminal_id = self.fixed_plan.terminal_stage_ids[-1]
                self._buffered_output = stage_outputs[terminal_id]
            self._has_buffered_output = True
            self._pending_args = ()
            self._pending_kwargs = {}
            return LifecycleEvent(
                kind=LifecycleEventKind.SUBMITTED,
                session_identity=action.session_identity,
                invocation_identity=action.invocation_identity,
                observed_at=now,
                action_token=action.token,
            )
        if action.kind is LifecycleActionKind.CONSUME_OUTPUT:
            return LifecycleEvent(
                kind=LifecycleEventKind.OUTPUT_CONSUMED,
                session_identity=action.session_identity,
                invocation_identity=action.invocation_identity,
                observed_at=now,
                action_token=action.token,
                output_index=action.output_index,
            )
        if action.kind is LifecycleActionKind.RELEASE_CAPACITY:
            self._pending_args = ()
            self._pending_kwargs = {}
            self._buffered_output = None
            self._has_buffered_output = False
            return LifecycleEvent(
                kind=LifecycleEventKind.CAPACITY_RELEASED,
                session_identity=action.session_identity,
                invocation_identity=action.invocation_identity,
                observed_at=now,
                action_token=action.token,
            )
        if action.kind is LifecycleActionKind.CANCEL:
            self._pending_args = ()
            self._pending_kwargs = {}
            self._buffered_output = None
            self._has_buffered_output = False
            return LifecycleEvent(
                kind=LifecycleEventKind.CANCELLED,
                session_identity=action.session_identity,
                invocation_identity=action.invocation_identity,
                observed_at=now,
                action_token=action.token,
            )
        if action.kind in {
            LifecycleActionKind.DRAIN_INVOCATION,
            LifecycleActionKind.DRAIN_SESSION,
        }:
            self.drained = True
            return LifecycleEvent(
                kind=LifecycleEventKind.DRAINED,
                session_identity=action.session_identity,
                invocation_identity=action.invocation_identity,
                observed_at=now,
                action_token=action.token,
            )
        if action.kind is LifecycleActionKind.CHECK_HEALTH:
            return LifecycleEvent(
                kind=LifecycleEventKind.HEALTHY,
                session_identity=action.session_identity,
                invocation_identity=action.invocation_identity,
                observed_at=now,
                action_token=action.token,
            )
        if action.kind is LifecycleActionKind.TEARDOWN:
            for replicas in self.replicas_by_stage.values():
                for replica in replicas:
                    replica.clear_state()
            self._pending_args = ()
            self._pending_kwargs = {}
            self._buffered_output = None
            self._has_buffered_output = False
            self.torn_down = True
            return LifecycleEvent(
                kind=LifecycleEventKind.TORN_DOWN,
                session_identity=action.session_identity,
                observed_at=now,
                action_token=action.token,
            )
        raise ValueError(f"Unsupported lifecycle action: {action.kind!r}")


class WorkflowExecutionStrategy(ABC):
    """Internal whole-plan execution-strategy contract (#70)."""

    strategy_name: WorkflowExecutionStrategyName

    @abstractmethod
    def validate(
        self,
        materialized_plan: MaterializedWorkflowPlan,
        *,
        requested_policy: str,
        reporting_policy: str | None = None,
        regions: Sequence[WorkflowRegionSpec] = (),
    ) -> PlanSelection:
        """Validate plan eligibility and return the bounded `PlanSelection`."""

    @abstractmethod
    def prepare(
        self,
        materialized_plan: MaterializedWorkflowPlan,
        *,
        selection: PlanSelection,
        run_identity: WorkflowRunIdentity | None = None,
        regions: Sequence[WorkflowRegionSpec] = (),
    ) -> PreparedWorkflowStrategy:
        """Prepare strategy state before invocation admission."""

    @abstractmethod
    def admit(
        self,
        prepared: PreparedWorkflowStrategy,
        *,
        invocation_identity: WorkflowInvocationIdentity | None = None,
    ) -> AdmittedWorkflowInvocation:
        """Admit a single workflow invocation against the prepared owner."""

    @abstractmethod
    def execute(
        self,
        admitted: AdmittedWorkflowInvocation,
        signature: WorkflowSignature,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> WorkflowStrategyHandle:
        """Start execution and return an opaque strategy handle."""

    @abstractmethod
    def consume_result(
        self,
        handle: Any,
        *,
        expected_run_identity: WorkflowRunIdentity | None = None,
    ) -> Any:
        """Consume the single invocation result from the strategy handle."""

    @abstractmethod
    def cancel(
        self,
        handle: Any,
        *,
        timeout_seconds: float = 5.0,
        expected_run_identity: WorkflowRunIdentity | None = None,
    ) -> None:
        """Cancel an in-flight invocation owned by `handle`."""

    @abstractmethod
    def drain(
        self,
        handle: Any,
        *,
        timeout_seconds: float = 5.0,
        expected_run_identity: WorkflowRunIdentity | None = None,
    ) -> None:
        """Drain in-flight strategy resources before teardown."""

    @abstractmethod
    def cleanup(
        self,
        handle: Any,
        *,
        expected_run_identity: WorkflowRunIdentity | None = None,
    ) -> None:
        """Release process-local strategy resources deterministically."""

    def observability(
        self,
        materialized_plan: MaterializedWorkflowPlan,
        selection: PlanSelection,
        *,
        regions: Sequence[WorkflowRegionSpec] = (),
        fallback_allowed_before_start: bool = False,
        effect_state: LifecycleEffectState = LifecycleEffectState.NOT_STARTED,
        retry_disposition: LifecycleRetryDisposition = LifecycleRetryDisposition.AUTOMATIC_ALLOWED,
        lifecycle_state: CompiledGraphLifecycleState | None = None,
    ) -> WorkflowStrategyObservability:
        """Build bounded strategy observability metadata even when reporting is disabled."""
        summary = materialized_plan.plan.summary()
        return WorkflowStrategyObservability(
            plan_fingerprint=materialized_plan.plan.fingerprint,
            definition_name=str(summary["definition_name"]),
            definition_revision=str(summary["definition_revision"]),
            requested_policy=selection.requested_policy,
            selected_strategy=selection.selected_strategy,
            reporting_policy=selection.reporting_policy,
            eligible_strategies=selection.eligible_strategies,
            rejections=selection.rejections,
            total_rejections=selection.total_rejections,
            regions=tuple(regions),
            fallback_allowed_before_start=fallback_allowed_before_start,
            effect_state=effect_state,
            retry_disposition=retry_disposition,
            lifecycle_snapshot=(
                lifecycle_snapshot(lifecycle_state) if lifecycle_state is not None else None
            ),
        )


def _validate_regions_for_v1(
    materialized_plan: MaterializedWorkflowPlan,
    regions: Sequence[WorkflowRegionSpec],
    *,
    selected_strategy: str,
    requested_policy: str,
) -> None:
    if not regions:
        return
    known_node_ids = {str(node["id"]) for node in materialized_plan.plan.manifest["nodes"]}
    seen_nodes: set[str] = set()
    for region in regions:
        unknown = [node_id for node_id in region.node_ids if node_id not in known_node_ids]
        if unknown:
            raise WorkflowPlanValidationError(
                f"Region {region.region_id!r} references unknown plan node(s): "
                + ", ".join(unknown)
            )
        overlap = seen_nodes.intersection(region.node_ids)
        if overlap:
            raise WorkflowPlanValidationError(
                f"Region {region.region_id!r} overlaps already-assigned node(s): "
                + ", ".join(sorted(overlap))
            )
        seen_nodes.update(region.node_ids)
    distinct_strategies = {region.requested_strategy for region in regions}
    if (
        len(distinct_strategies) > 1
        or seen_nodes != known_node_ids
        or next(iter(distinct_strategies)) != selected_strategy
    ):
        rejection = PlanRejection(
            selected_strategy,
            "MIXED_REGIONS_UNSUPPORTED",
            "regions",
            "V1 whole-plan strategies do not execute partial or mixed dynamic/static regions (#96)",
        )
        raise WorkflowStrategySelectionError(
            f"{rejection.code}: {rejection.message}",
            requested_policy=requested_policy,
            rejections=(rejection,),
        )


def _default_run_identity() -> WorkflowRunIdentity:
    task_context = get_current_task_context()
    if task_context is not None:
        created = WorkflowRunIdentity.create(task_context)
        if created is not None:
            return created
    return WorkflowRunIdentity(
        task_execution_pk=1,
        attempt_number=1,
        execution_generation=1,
        run_id="local-workflow-run",
    )


class LocalWorkflowExecutionStrategy(WorkflowExecutionStrategy):
    """Deterministic in-process strategy for sync workers and unit tests."""

    strategy_name = WorkflowExecutionStrategyName.LOCAL

    def validate(
        self,
        materialized_plan: MaterializedWorkflowPlan,
        *,
        requested_policy: str,
        reporting_policy: str | None = None,
        regions: Sequence[WorkflowRegionSpec] = (),
    ) -> PlanSelection:
        _validate_regions_for_v1(
            materialized_plan,
            regions,
            selected_strategy=self.strategy_name.value,
            requested_policy=requested_policy,
        )
        return materialized_plan.plan.eligibility.select(
            self.strategy_name.value,
            requested_policy=requested_policy,
            reporting_policy="disabled" if reporting_policy is None else reporting_policy,
        )

    def prepare(
        self,
        materialized_plan: MaterializedWorkflowPlan,
        *,
        selection: PlanSelection,
        run_identity: WorkflowRunIdentity | None = None,
        regions: Sequence[WorkflowRegionSpec] = (),
    ) -> PreparedWorkflowStrategy:
        return PreparedWorkflowStrategy(
            strategy_name=self.strategy_name.value,
            materialized_plan=materialized_plan,
            selection=selection,
            run_identity=run_identity,
            regions=tuple(regions),
        )

    def admit(
        self,
        prepared: PreparedWorkflowStrategy,
        *,
        invocation_identity: WorkflowInvocationIdentity | None = None,
    ) -> AdmittedWorkflowInvocation:
        run_id = prepared.run_identity or _default_run_identity()
        inv_id = invocation_identity or WorkflowInvocationIdentity(
            run_identity=run_id,
            invocation_id=f"local-{uuid.uuid4().hex[:16]}",
        )
        return AdmittedWorkflowInvocation(
            prepared=prepared,
            invocation_identity=inv_id,
        )

    def execute(
        self,
        admitted: AdmittedWorkflowInvocation,
        signature: WorkflowSignature,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> LocalStrategyHandle:
        from django_ray.workflows import _LocalExecutor

        admitted.prepared.materialized_plan.plan.assert_owner_fingerprint(
            admitted.prepared.materialized_plan.plan.fingerprint
        )
        executor = _LocalExecutor(admitted.prepared.materialized_plan)
        executor.bind_plan(
            admitted.prepared.materialized_plan,
            requested_policy=admitted.prepared.selection.requested_policy,
            reporting_policy="disabled",
        )
        try:
            submission = signature._submit(executor, args, kwargs, "0", ())
            result = executor.resolve(submission.value)
        except BaseException:
            executor.finish_progress(failed=True)
            raise
        executor.finish_progress()
        return LocalStrategyHandle(
            plan_fingerprint=admitted.prepared.materialized_plan.plan.fingerprint,
            run_identity=admitted.prepared.run_identity,
            invocation_id=admitted.invocation_identity.invocation_id,
            opaque_token=f"local-handle:{admitted.invocation_identity.invocation_id}",
            _result=result,
        )

    def consume_result(
        self,
        handle: LocalStrategyHandle,
        *,
        expected_run_identity: WorkflowRunIdentity | None = None,
    ) -> Any:
        verify_run_callback_identity(expected_run_identity, handle.run_identity)
        if handle._consumed:
            raise RuntimeError("LocalStrategyHandle result has already been consumed")
        if handle._cancelled or handle._cleaned_up:
            raise RuntimeError("LocalStrategyHandle is no longer active")
        handle._consumed = True
        value = handle._result
        handle._result = None
        return value

    def cancel(
        self,
        handle: LocalStrategyHandle,
        *,
        timeout_seconds: float = 5.0,
        expected_run_identity: WorkflowRunIdentity | None = None,
    ) -> None:
        del timeout_seconds
        verify_run_callback_identity(expected_run_identity, handle.run_identity)
        handle._cancelled = True
        handle._result = None

    def drain(
        self,
        handle: LocalStrategyHandle,
        *,
        timeout_seconds: float = 5.0,
        expected_run_identity: WorkflowRunIdentity | None = None,
    ) -> None:
        del timeout_seconds
        verify_run_callback_identity(expected_run_identity, handle.run_identity)
        handle._drained = True

    def cleanup(
        self,
        handle: LocalStrategyHandle,
        *,
        expected_run_identity: WorkflowRunIdentity | None = None,
    ) -> None:
        verify_run_callback_identity(expected_run_identity, handle.run_identity)
        handle._result = None
        handle._cleaned_up = True


class DynamicTasksWorkflowExecutionStrategy(WorkflowExecutionStrategy):
    """Default Ray dynamic task-graph execution strategy (#70)."""

    strategy_name = WorkflowExecutionStrategyName.DYNAMIC_TASKS

    def validate(
        self,
        materialized_plan: MaterializedWorkflowPlan,
        *,
        requested_policy: str,
        reporting_policy: str | None = None,
        regions: Sequence[WorkflowRegionSpec] = (),
    ) -> PlanSelection:
        _validate_regions_for_v1(
            materialized_plan,
            regions,
            selected_strategy=self.strategy_name.value,
            requested_policy=requested_policy,
        )
        return materialized_plan.plan.eligibility.select(
            self.strategy_name.value,
            requested_policy=requested_policy,
            reporting_policy=reporting_policy,
        )

    def prepare(
        self,
        materialized_plan: MaterializedWorkflowPlan,
        *,
        selection: PlanSelection,
        run_identity: WorkflowRunIdentity | None = None,
        regions: Sequence[WorkflowRegionSpec] = (),
    ) -> PreparedWorkflowStrategy:
        return PreparedWorkflowStrategy(
            strategy_name=self.strategy_name.value,
            materialized_plan=materialized_plan,
            selection=selection,
            run_identity=run_identity,
            regions=tuple(regions),
        )

    def admit(
        self,
        prepared: PreparedWorkflowStrategy,
        *,
        invocation_identity: WorkflowInvocationIdentity | None = None,
    ) -> AdmittedWorkflowInvocation:
        run_id = prepared.run_identity or _default_run_identity()
        inv_id = invocation_identity or WorkflowInvocationIdentity(
            run_identity=run_id,
            invocation_id=f"dynamic-{uuid.uuid4().hex[:16]}",
        )
        return AdmittedWorkflowInvocation(
            prepared=prepared,
            invocation_identity=inv_id,
        )

    def execute(
        self,
        admitted: AdmittedWorkflowInvocation,
        signature: WorkflowSignature,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        executor: Any | None = None,
    ) -> DynamicTasksStrategyHandle:
        from django_ray.workflows import _get_executor

        admitted.prepared.materialized_plan.plan.assert_owner_fingerprint(
            admitted.prepared.materialized_plan.plan.fingerprint
        )
        ray_executor = executor or _get_executor(True)
        ray_executor.bind_plan(
            admitted.prepared.materialized_plan,
            requested_policy=admitted.prepared.selection.requested_policy,
            reporting_policy=admitted.prepared.selection.reporting_policy,
        )
        try:
            submission = signature._submit(ray_executor, args, kwargs, "0", ())
        except BaseException:
            ray_executor.finish_progress(failed=True)
            raise
        return DynamicTasksStrategyHandle(
            plan_fingerprint=admitted.prepared.materialized_plan.plan.fingerprint,
            run_identity=admitted.prepared.run_identity,
            invocation_id=admitted.invocation_identity.invocation_id,
            opaque_token=f"dynamic-handle:{admitted.invocation_identity.invocation_id}",
            _executor=ray_executor,
            _submission=submission,
        )

    def consume_result(
        self,
        handle: DynamicTasksStrategyHandle,
        *,
        expected_run_identity: WorkflowRunIdentity | None = None,
    ) -> Any:
        verify_run_callback_identity(expected_run_identity, handle.run_identity)
        if handle._consumed:
            raise RuntimeError("DynamicTasksStrategyHandle result has already been consumed")
        if handle._cancelled or handle._cleaned_up:
            raise RuntimeError("DynamicTasksStrategyHandle is no longer active")
        try:
            if not handle._resolved:
                handle._resolved_result = handle._executor.resolve(handle._submission.value)
                handle._resolved = True
        except BaseException:
            handle._executor.finish_progress(failed=True)
            raise
        handle._executor.finish_progress()
        handle._consumed = True
        value = handle._resolved_result
        handle._resolved_result = None
        return value

    def cancel(
        self,
        handle: DynamicTasksStrategyHandle,
        *,
        timeout_seconds: float = 5.0,
        expected_run_identity: WorkflowRunIdentity | None = None,
    ) -> None:
        verify_run_callback_identity(expected_run_identity, handle.run_identity)
        if handle._cancelled:
            return
        handle._cancelled = True
        if handle._executor is not None and handle._submission is not None:
            cancel_fn = getattr(handle._executor, "cancel_and_drain", None)
            if callable(cancel_fn):
                cancel_fn([handle._submission.value], timeout_seconds=timeout_seconds)
            handle._executor.finish_progress(failed=True)

    def drain(
        self,
        handle: DynamicTasksStrategyHandle,
        *,
        timeout_seconds: float = 5.0,
        expected_run_identity: WorkflowRunIdentity | None = None,
    ) -> None:
        del timeout_seconds
        verify_run_callback_identity(expected_run_identity, handle.run_identity)
        handle._drained = True

    def cleanup(
        self,
        handle: DynamicTasksStrategyHandle,
        *,
        expected_run_identity: WorkflowRunIdentity | None = None,
    ) -> None:
        verify_run_callback_identity(expected_run_identity, handle.run_identity)
        handle._submission = None
        handle._resolved_result = None
        handle._executor = None
        handle._cleaned_up = True


class CompiledGraphExecutionStrategy(WorkflowExecutionStrategy):
    """Explicit default-off Compiled Graph strategy for eligible static plans (#72).

    Dynamic execution remains the unconditional default. Selecting this strategy
    requires both ``DJANGO_RAY["COMPILED_GRAPH_EXPERIMENTAL_ENABLED"] = True``
    and explicit per-run selection (`requested_policy="compiled_graph"`).
    """

    strategy_name = WorkflowExecutionStrategyName.COMPILED_GRAPH

    def __init__(
        self,
        *,
        signature: WorkflowSignature,
        pool_specs: Mapping[str, StaticActorPoolSpec] | None = None,
        adapter_factory: Callable[[FixedActorGraphPlan], InProcessFixedActorAdapter] | None = None,
        capability_decision: CompiledGraphCapabilityDecision | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._signature = signature
        self._pool_specs = dict(pool_specs or {})
        self._adapter_factory = adapter_factory or (
            lambda fixed_plan: InProcessFixedActorAdapter(fixed_plan, clock=clock)
        )
        self._capability_decision = capability_decision
        self._clock = clock

    def evaluate_eligibility(
        self,
        materialized_plan: MaterializedWorkflowPlan,
        *,
        config: Mapping[str, Any] | None = None,
    ) -> tuple[PlanEligibility, FixedActorGraphPlan | None]:
        """Evaluate experimental gate, fixed-actor builder, and `#86` capability support."""
        rejections: list[PlanRejection] = []
        if not compiled_graph_experimental_enabled(config):
            rejections.append(
                PlanRejection(
                    "compiled_graph",
                    "EXPERIMENTAL_GATE_DISABLED",
                    "settings.COMPILED_GRAPH_EXPERIMENTAL_ENABLED",
                    "Compiled Graph experimental strategy is disabled by default; "
                    "set DJANGO_RAY['COMPILED_GRAPH_EXPERIMENTAL_ENABLED'] = True",
                )
            )

        fixed_plan, builder_rejections = build_fixed_actor_graph_plan(
            self._signature,
            materialized_plan,
            pool_specs=self._pool_specs,
        )
        rejections.extend(builder_rejections)

        manifest = materialized_plan.plan.manifest
        cg_settings = manifest.get("strategy_requirements", {}).get("compiled_graph", {})
        if (
            cg_settings.get("lifecycle_protocol_version")
            != COMPILED_GRAPH_LIFECYCLE_PROTOCOL_VERSION
        ):
            rejections.append(
                PlanRejection(
                    "compiled_graph",
                    "UNSUPPORTED_LIFECYCLE_PROTOCOL",
                    "strategy_requirements.compiled_graph.lifecycle_protocol_version",
                    "Compiled Graph requires lifecycle protocol v1",
                )
            )
        if cg_settings.get("transport") != CompiledGraphTransport.CPU_SHARED_MEMORY.value:
            rejections.append(
                PlanRejection(
                    "compiled_graph",
                    "UNSUPPORTED_TRANSPORT",
                    "strategy_requirements.compiled_graph.transport",
                    "Only cpu-shared-memory transport is supported by the V1 Compiled Graph boundary",
                )
            )

        decision = self._capability_decision
        if decision is None:
            cg_compat = manifest.get("compatibility", {}).get("compiled_graph", {})
            topology = cg_compat.get("topology") or CompiledGraphTopology.RAY_JOB_DRIVER.value
            submission_transport = (
                cg_compat.get("submission_transport")
                or CompiledGraphSubmissionTransport.RAY_JOB.value
            )
            runtime_dict = cg_compat.get("runtime", {})
            runtime = (
                CompiledGraphRuntimeIdentity(
                    ray_version=runtime_dict.get("ray_version"),
                    python_version=runtime_dict.get("python_version"),
                    operating_system=runtime_dict.get("operating_system"),
                    architecture=runtime_dict.get("architecture"),
                    deployment_profile=runtime_dict.get("deployment_profile"),
                    dependency_profile=runtime_dict.get("dependency_profile"),
                )
                if isinstance(runtime_dict, Mapping)
                else detect_compiled_graph_runtime()
            )
            decision = evaluate_compiled_graph_support(
                topology,
                CompiledGraphTransport.CPU_SHARED_MEMORY,
                submission_transport=submission_transport,
                runtime=runtime,
            )
        if not decision.eligible or not decision.verified:
            rejections.append(
                PlanRejection(
                    "compiled_graph",
                    decision.plan_rejection_code or "CAPABILITY_UNVERIFIED",
                    "compatibility.compiled_graph",
                    decision.message,
                )
            )

        deduped = sorted(
            {(r.strategy, r.code, r.path, r.message): r for r in rejections}.values(),
            key=lambda item: (item.strategy, item.code, item.path),
        )
        eligible = (
            ("compiled_graph", "dynamic_tasks", "local")
            if not deduped and fixed_plan is not None
            else ("dynamic_tasks", "local")
        )
        return (
            PlanEligibility(
                eligible_strategies=eligible,
                rejections=tuple(deduped[:MAX_REJECTIONS]),
                total_rejections=len(deduped),
            ),
            fixed_plan if not deduped else None,
        )

    def validate(
        self,
        materialized_plan: MaterializedWorkflowPlan,
        *,
        requested_policy: str,
        reporting_policy: str | None = None,
        regions: Sequence[WorkflowRegionSpec] = (),
        config: Mapping[str, Any] | None = None,
    ) -> PlanSelection:
        if requested_policy != WorkflowRequestedStrategyPolicy.COMPILED_GRAPH.value:
            raise WorkflowStrategySelectionError(
                "CompiledGraphExecutionStrategy requires explicit requested_policy='compiled_graph'",
                requested_policy=requested_policy,
            )
        _validate_regions_for_v1(
            materialized_plan,
            regions,
            selected_strategy=self.strategy_name.value,
            requested_policy=requested_policy,
        )
        eligibility, _ = self.evaluate_eligibility(materialized_plan, config=config)
        if self.strategy_name.value not in eligibility.eligible_strategies:
            first_message = (
                eligibility.rejections[0].message
                if eligibility.rejections
                else "Workflow plan is not eligible for compiled_graph"
            )
            raise WorkflowStrategySelectionError(
                f"Execution strategy 'compiled_graph' rejected before allocation: {first_message}",
                requested_policy=requested_policy,
                rejections=eligibility.rejections,
            )
        return eligibility.select(
            self.strategy_name.value,
            requested_policy=requested_policy,
            reporting_policy=reporting_policy,
        )

    def _drive_action(
        self,
        state: CompiledGraphLifecycleState,
        event: LifecycleEvent,
        adapter: InProcessFixedActorAdapter | CompiledGraphLifecycleAdapter,
    ) -> CompiledGraphLifecycleState:
        transition = reduce_lifecycle(state, event)
        if isinstance(transition, LifecycleRejection):
            raise RuntimeError(
                f"Compiled Graph lifecycle rejected {event.kind.value}: "
                f"{transition.code.value}: {transition.message}"
            )
        current = transition.state
        pending_actions = list(transition.actions)
        while pending_actions:
            action = pending_actions.pop(0)
            callback_event = adapter.dispatch(action)
            callback_transition = reduce_lifecycle(current, callback_event)
            if isinstance(callback_transition, LifecycleRejection):
                raise RuntimeError(
                    f"Compiled Graph lifecycle callback rejected {callback_event.kind.value}: "
                    f"{callback_transition.code.value}: {callback_transition.message}"
                )
            current = callback_transition.state
            pending_actions.extend(callback_transition.actions)
        return current

    def prepare(
        self,
        materialized_plan: MaterializedWorkflowPlan,
        *,
        selection: PlanSelection,
        run_identity: WorkflowRunIdentity | None = None,
        regions: Sequence[WorkflowRegionSpec] = (),
        config: Mapping[str, Any] | None = None,
    ) -> PreparedWorkflowStrategy:
        eligibility, fixed_plan = self.evaluate_eligibility(materialized_plan, config=config)
        if fixed_plan is None or self.strategy_name.value not in eligibility.eligible_strategies:
            raise WorkflowStrategySelectionError(
                "Compiled Graph preparation refused an ineligible plan",
                requested_policy=selection.requested_policy,
                rejections=eligibility.rejections,
            )
        session_identity = run_identity or _default_run_identity()
        now = float(self._clock())
        deadlines = LifecycleDeadlines(
            outer=now + 60.0,
            admission=now + 10.0,
            submission=now + 30.0,
            result=now + 30.0,
            cancellation=now + 10.0,
            drain=now + 10.0,
            teardown=now + 10.0,
        )
        state = initial_session(
            session_identity=session_identity,
            deadlines=deadlines,
            capacity=LifecycleCapacity(),
        )
        adapter = self._adapter_factory(fixed_plan)
        state = self._drive_action(
            state,
            LifecycleEvent(
                kind=LifecycleEventKind.VALIDATION_REQUESTED,
                session_identity=session_identity,
                observed_at=float(self._clock()),
            ),
            adapter,
        )
        state = self._drive_action(
            state,
            LifecycleEvent(
                kind=LifecycleEventKind.PREPARATION_REQUESTED,
                session_identity=session_identity,
                observed_at=float(self._clock()),
            ),
            adapter,
        )
        if state.session_state is not CompiledGraphSessionState.READY:
            raise RuntimeError(
                f"Compiled Graph session failed preparation: {state.primary_outcome}"
            )
        prepared = PreparedWorkflowStrategy(
            strategy_name=self.strategy_name.value,
            materialized_plan=materialized_plan,
            selection=selection,
            run_identity=session_identity,
            regions=tuple(regions),
            fixed_actor_plan=fixed_plan,
            lifecycle_state=state,
            effect_state=state.effect_state,
        )
        object.__setattr__(prepared, "_adapter", adapter)
        return prepared

    def admit(
        self,
        prepared: PreparedWorkflowStrategy,
        *,
        invocation_identity: WorkflowInvocationIdentity | None = None,
    ) -> AdmittedWorkflowInvocation:
        assert prepared.run_identity is not None
        assert prepared.lifecycle_state is not None
        adapter = prepared._adapter
        inv_id = invocation_identity or WorkflowInvocationIdentity(
            run_identity=prepared.run_identity,
            invocation_id=f"cg-{uuid.uuid4().hex[:16]}",
        )
        state = self._drive_action(
            prepared.lifecycle_state,
            LifecycleEvent(
                kind=LifecycleEventKind.ADMISSION_REQUESTED,
                session_identity=prepared.run_identity,
                invocation_identity=inv_id,
                declared_outputs=1,
                observed_at=float(self._clock()),
            ),
            adapter,
        )
        if state.invocation_state is not CompiledGraphInvocationState.ADMITTED:
            raise RuntimeError(
                f"Compiled Graph invocation admission failed: {state.primary_outcome}"
            )
        admitted = AdmittedWorkflowInvocation(
            prepared=prepared,
            invocation_identity=inv_id,
            lifecycle_state=state,
        )
        object.__setattr__(admitted, "_adapter", adapter)
        return admitted

    def execute(
        self,
        admitted: AdmittedWorkflowInvocation,
        signature: WorkflowSignature,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> CompiledGraphStrategyHandle:
        del signature
        assert admitted.prepared.run_identity is not None
        assert admitted.prepared.fixed_actor_plan is not None
        assert admitted.lifecycle_state is not None
        admitted.prepared.materialized_plan.plan.assert_owner_fingerprint(
            admitted.prepared.fixed_actor_plan.plan_fingerprint
        )
        adapter: InProcessFixedActorAdapter = admitted._adapter
        adapter.bind_invocation_inputs(args, kwargs)
        state = self._drive_action(
            admitted.lifecycle_state,
            LifecycleEvent(
                kind=LifecycleEventKind.SUBMISSION_REQUESTED,
                session_identity=admitted.prepared.run_identity,
                invocation_identity=admitted.invocation_identity,
                observed_at=float(self._clock()),
            ),
            adapter,
        )
        return CompiledGraphStrategyHandle(
            plan_fingerprint=admitted.prepared.materialized_plan.plan.fingerprint,
            run_identity=admitted.prepared.run_identity,
            invocation_identity=admitted.invocation_identity,
            opaque_token=f"compiled-graph-handle:{admitted.invocation_identity.invocation_id}",
            fixed_actor_plan=admitted.prepared.fixed_actor_plan,
            lifecycle_state=state,
            _adapter=adapter,
            _output_ready=state.invocation_state is CompiledGraphInvocationState.RUNNING,
        )

    def consume_result(
        self,
        handle: CompiledGraphStrategyHandle,
        *,
        expected_run_identity: WorkflowRunIdentity | None = None,
    ) -> Any:
        verify_run_callback_identity(expected_run_identity, handle.run_identity)
        if handle._consumed:
            raise RuntimeError("CompiledGraphStrategyHandle output has already been consumed")
        if handle._cancelled or handle._cleaned_up:
            raise RuntimeError("CompiledGraphStrategyHandle is no longer active")
        state = self._drive_action(
            handle.lifecycle_state,
            LifecycleEvent(
                kind=LifecycleEventKind.CONSUMPTION_REQUESTED,
                session_identity=handle.run_identity,
                invocation_identity=handle.invocation_identity,
                output_index=0,
                observed_at=float(self._clock()),
            ),
            handle._adapter,
        )
        handle.lifecycle_state = state
        if state.primary_outcome is not LifecycleOutcome.SUCCEEDED:
            raise RuntimeError(f"Compiled Graph output consumption failed: {state.primary_outcome}")
        handle._consumed = True
        if isinstance(handle._adapter, InProcessFixedActorAdapter):
            return handle._adapter.pop_consumed_output()
        return handle._output_value

    def cancel(
        self,
        handle: CompiledGraphStrategyHandle,
        *,
        timeout_seconds: float = 5.0,
        expected_run_identity: WorkflowRunIdentity | None = None,
    ) -> None:
        del timeout_seconds
        verify_run_callback_identity(expected_run_identity, handle.run_identity)
        if handle._cancelled or handle._consumed:
            return
        handle.lifecycle_state = self._drive_action(
            handle.lifecycle_state,
            LifecycleEvent(
                kind=LifecycleEventKind.CANCELLATION_REQUESTED,
                session_identity=handle.run_identity,
                invocation_identity=handle.invocation_identity,
                observed_at=float(self._clock()),
            ),
            handle._adapter,
        )
        handle._cancelled = True

    def drain(
        self,
        handle: CompiledGraphStrategyHandle,
        *,
        timeout_seconds: float = 5.0,
        expected_run_identity: WorkflowRunIdentity | None = None,
    ) -> None:
        del timeout_seconds
        verify_run_callback_identity(expected_run_identity, handle.run_identity)
        if handle._drained:
            return
        if handle.lifecycle_state.session_state is CompiledGraphSessionState.READY:
            handle.lifecycle_state = self._drive_action(
                handle.lifecycle_state,
                LifecycleEvent(
                    kind=LifecycleEventKind.DRAIN_REQUESTED,
                    session_identity=handle.run_identity,
                    observed_at=float(self._clock()),
                ),
                handle._adapter,
            )
        handle._drained = True

    def cleanup(
        self,
        handle: CompiledGraphStrategyHandle,
        *,
        expected_run_identity: WorkflowRunIdentity | None = None,
    ) -> None:
        verify_run_callback_identity(expected_run_identity, handle.run_identity)
        if handle._cleaned_up:
            return
        if not handle._drained:
            self.drain(handle, expected_run_identity=expected_run_identity)
        if handle.lifecycle_state.session_state is not CompiledGraphSessionState.TORN_DOWN:
            handle.lifecycle_state = self._drive_action(
                handle.lifecycle_state,
                LifecycleEvent(
                    kind=LifecycleEventKind.TEARDOWN_REQUESTED,
                    session_identity=handle.run_identity,
                    observed_at=float(self._clock()),
                ),
                handle._adapter,
            )
        handle._output_value = None
        handle._cleaned_up = True


def resolve_execution_strategy(
    signature: WorkflowSignature,
    materialized_plan: MaterializedWorkflowPlan,
    *,
    use_ray: bool | None,
    requested_policy: str | None = None,
    reporting_policy: str | None = None,
    regions: Sequence[WorkflowRegionSpec] = (),
    pool_specs: Mapping[str, StaticActorPoolSpec] | None = None,
    compiled_graph_capability_decision: CompiledGraphCapabilityDecision | None = None,
    config: Mapping[str, Any] | None = None,
) -> tuple[WorkflowExecutionStrategy, PlanSelection]:
    """Select and validate the concrete `WorkflowExecutionStrategy` for one run (#70/#72).

    - ``requested_policy=None`` or ``"auto"`` never selects ``compiled_graph``,
      even when ``COMPILED_GRAPH_EXPERIMENTAL_ENABLED=True``.
    - Explicit ``requested_policy="compiled_graph"`` requires both the global
      experimental gate and full static/capability eligibility before any allocation.
    """
    policy = requested_policy or (
        WorkflowRequestedStrategyPolicy.LOCAL.value
        if use_ray is False
        else (
            WorkflowRequestedStrategyPolicy.DYNAMIC_TASKS.value
            if use_ray is True
            else WorkflowRequestedStrategyPolicy.AUTO.value
        )
    )
    if policy not in {item.value for item in WorkflowRequestedStrategyPolicy}:
        raise WorkflowPlanValidationError(
            f"Unsupported workflow execution strategy policy: {policy!r}"
        )

    if policy == WorkflowRequestedStrategyPolicy.COMPILED_GRAPH.value:
        if use_ray is False:
            raise WorkflowPlanValidationError(
                "requested_policy='compiled_graph' cannot be combined with use_ray=False"
            )
        cg_strategy = CompiledGraphExecutionStrategy(
            signature=signature,
            pool_specs=pool_specs,
            capability_decision=compiled_graph_capability_decision,
        )
        selection = cg_strategy.validate(
            materialized_plan,
            requested_policy=policy,
            reporting_policy=reporting_policy,
            regions=regions,
            config=config,
        )
        return cg_strategy, selection

    if policy == WorkflowRequestedStrategyPolicy.STATIC_ACTORS.value:
        rejections = tuple(
            r
            for r in materialized_plan.plan.eligibility.rejections
            if r.strategy == "static_actors"
        )
        raise WorkflowStrategySelectionError(
            "Execution strategy 'static_actors' is not eligible for this workflow plan",
            requested_policy=policy,
            rejections=rejections,
        )

    if policy == WorkflowRequestedStrategyPolicy.LOCAL.value:
        local_strategy = LocalWorkflowExecutionStrategy()
        selection = local_strategy.validate(
            materialized_plan,
            requested_policy=policy,
            reporting_policy="disabled" if reporting_policy is None else reporting_policy,
            regions=regions,
        )
        return local_strategy, selection

    if policy == WorkflowRequestedStrategyPolicy.DYNAMIC_TASKS.value:
        dynamic_strategy = DynamicTasksWorkflowExecutionStrategy()
        selection = dynamic_strategy.validate(
            materialized_plan,
            requested_policy=policy,
            reporting_policy=reporting_policy,
            regions=regions,
        )
        return dynamic_strategy, selection

    # AUTO mode: choose dynamic_tasks when Ray is available/initialized, else local.
    # AUTO mode unconditionally excludes compiled_graph (#72).
    from django_ray.workflows import _get_executor, _LocalExecutor

    probe_executor = _get_executor(use_ray)
    if isinstance(probe_executor, _LocalExecutor):
        local_strategy = LocalWorkflowExecutionStrategy()
        selection = local_strategy.validate(
            materialized_plan,
            requested_policy=WorkflowRequestedStrategyPolicy.AUTO.value,
            reporting_policy="disabled",
            regions=regions,
        )
        return local_strategy, selection

    dynamic_strategy = DynamicTasksWorkflowExecutionStrategy()
    selection = dynamic_strategy.validate(
        materialized_plan,
        requested_policy=WorkflowRequestedStrategyPolicy.AUTO.value,
        reporting_policy=reporting_policy,
        regions=regions,
    )
    return dynamic_strategy, selection


__all__ = [
    "AdmittedWorkflowInvocation",
    "CompiledGraphExecutionStrategy",
    "CompiledGraphStrategyHandle",
    "DynamicTasksStrategyHandle",
    "DynamicTasksWorkflowExecutionStrategy",
    "FixedActorGraphPlan",
    "InProcessFixedActorAdapter",
    "LocalStrategyHandle",
    "LocalWorkflowExecutionStrategy",
    "PreparedWorkflowStrategy",
    "StaleWorkflowRunCallbackError",
    "StaticActorPoolSpec",
    "StaticActorStageSpec",
    "WorkflowExecutionStrategy",
    "WorkflowExecutionStrategyName",
    "WorkflowRegionSpec",
    "WorkflowRequestedStrategyPolicy",
    "WorkflowStrategyFallbackError",
    "WorkflowStrategyHandle",
    "WorkflowStrategyObservability",
    "WorkflowStrategySelectionError",
    "attempt_strategy_fallback",
    "build_fixed_actor_graph_plan",
    "compiled_graph_experimental_enabled",
    "resolve_execution_strategy",
    "verify_run_callback_identity",
]
