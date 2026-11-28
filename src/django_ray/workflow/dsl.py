"""Workflow DSL 2.0 dry-run plan printer and ergonomic combinators.

This module layers cleanly below :mod:`django_ray.workflows` (zero circular
imports) and alongside :mod:`django_ray.workflow.plans`. It provides:

- :func:`explain_workflow`: secret-free, side-effect-free dry-run inspection of
  any ``Step``, ``Chain``, ``Group``, ``Map``, :class:`WorkflowExpr`, or
  :class:`WorkflowBuilder` without executing callables or touching Ray/DB.
- :class:`WorkflowExpr`: immutable composable wrapper supporting ``|``
  (sequential pipe -> ``Chain``), ``&`` (parallel fan-out -> ``Group``),
  ``.map(...)``, ``.with_fallback(...)``, ``.explain(...)``, ``.build()``,
  and ``.run(...)``.
- :func:`pipe`, :func:`parallel`, and :func:`branch`: functional combinators
  with eager validation and clean canonical callable identities.
"""

from __future__ import annotations

import contextvars
import inspect
import math
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence, Sized
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeGuard, cast, overload, runtime_checkable

from django.core.exceptions import ImproperlyConfigured

from django_ray.redaction import redact_text
from django_ray.runtime.import_utils import resolve_workflow_callable
from django_ray.workflow._types import (
    ChainSignatureProtocol,
    GroupSignatureProtocol,
    StepSignatureProtocol,
    WorkflowSignatureProtocol,
    get_workflow_executor,
    is_local_workflow_executor,
)
from django_ray.workflow.contracts import WorkflowDefinitionKind
from django_ray.workflow.plans import (
    PlannableWorkflowSignature,
    WorkflowPlanBuildContext,
    WorkflowPlanValidationError,
    materialize_workflow_plan,
)

EXPLAIN_SCHEMA = "django-ray.workflow-explain"
EXPLAIN_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class _WorkflowDslPrimitives:
    """Registered workflow signature constructors owned by ``django_ray.workflows``."""

    workflow_signature_cls: type[Any]
    step_cls: type[Any]
    chain_cls: type[Any]
    group_cls: type[Any]
    map_cls: type[Any]
    definition_error_cls: type[Exception]
    step_fn: Callable[..., Any]
    chain_fn: Callable[..., Any]
    group_fn: Callable[..., Any]
    map_step_fn: Callable[..., Any]


_WORKFLOW_DSL_PRIMITIVES: _WorkflowDslPrimitives | None = None


def register_workflow_dsl_primitives(
    *,
    workflow_signature_cls: type[Any],
    step_cls: type[Any],
    chain_cls: type[Any],
    group_cls: type[Any],
    map_cls: type[Any],
    definition_error_cls: type[Exception],
    step_fn: Callable[..., Any],
    chain_fn: Callable[..., Any],
    group_fn: Callable[..., Any],
    map_step_fn: Callable[..., Any],
) -> None:
    """Register workflow signature classes and builders owned by ``django_ray.workflows``."""
    global _WORKFLOW_DSL_PRIMITIVES
    _WORKFLOW_DSL_PRIMITIVES = _WorkflowDslPrimitives(
        workflow_signature_cls=workflow_signature_cls,
        step_cls=step_cls,
        chain_cls=chain_cls,
        group_cls=group_cls,
        map_cls=map_cls,
        definition_error_cls=definition_error_cls,
        step_fn=step_fn,
        chain_fn=chain_fn,
        group_fn=group_fn,
        map_step_fn=map_step_fn,
    )


def _primitives() -> _WorkflowDslPrimitives:
    if _WORKFLOW_DSL_PRIMITIVES is None:
        raise RuntimeError(
            "Workflow DSL primitives are not registered; import django_ray.workflows first"
        )
    return _WORKFLOW_DSL_PRIMITIVES


def _definition_error(message: str) -> Exception:
    if _WORKFLOW_DSL_PRIMITIVES is not None:
        return _WORKFLOW_DSL_PRIMITIVES.definition_error_cls(message)
    return ValueError(message)


def _is_workflow_signature(value: Any) -> TypeGuard[WorkflowSignatureProtocol]:
    if _WORKFLOW_DSL_PRIMITIVES is not None:
        return isinstance(value, _WORKFLOW_DSL_PRIMITIVES.workflow_signature_cls)
    return isinstance(value, WorkflowSignatureProtocol)


def _is_step(value: Any) -> TypeGuard[StepSignatureProtocol]:
    if _WORKFLOW_DSL_PRIMITIVES is not None:
        return isinstance(value, _WORKFLOW_DSL_PRIMITIVES.step_cls)
    return getattr(value, "_workflow_definition_kind", None) is WorkflowDefinitionKind.STEP


def _is_chain(value: Any) -> TypeGuard[ChainSignatureProtocol]:
    if _WORKFLOW_DSL_PRIMITIVES is not None:
        return isinstance(value, _WORKFLOW_DSL_PRIMITIVES.chain_cls)
    return getattr(value, "_workflow_definition_kind", None) is WorkflowDefinitionKind.CHAIN


def _is_group(value: Any) -> TypeGuard[GroupSignatureProtocol]:
    if _WORKFLOW_DSL_PRIMITIVES is not None:
        return isinstance(value, _WORKFLOW_DSL_PRIMITIVES.group_cls)
    return getattr(value, "_workflow_definition_kind", None) is WorkflowDefinitionKind.GROUP


def _is_map(value: Any) -> bool:
    if _WORKFLOW_DSL_PRIMITIVES is not None:
        return isinstance(value, _WORKFLOW_DSL_PRIMITIVES.map_cls)
    return getattr(value, "_workflow_definition_kind", None) is WorkflowDefinitionKind.MAP


_SAFE_RAY_OPTION_KEYS = frozenset(
    {
        "accelerator_type",
        "max_retries",
        "memory",
        "name",
        "num_cpus",
        "num_gpus",
        "resources",
        "scheduling_strategy",
    }
)
_EXPLAIN_ERROR_MAX_CHARS = 256


@runtime_checkable
class WorkflowBuilder(Protocol):
    """Protocol for workflow builders that materialize a ``WorkflowSignature``."""

    def build(self) -> Any: ...


@dataclass(frozen=True, slots=True)
class _BranchNode:
    """Declarative conditional routing node with canonical predicate identity."""

    predicate_path: str
    on_true: _DslNode
    on_false: _DslNode
    predicate_callable: Callable[..., Any] | None = None


@dataclass(frozen=True, slots=True)
class _FallbackNode:
    """Declarative fallback node catching explicit exception subclasses."""

    primary: _DslNode
    fallback: _DslNode
    exception_types: tuple[type[Exception], ...]
    pass_error: bool = False


@dataclass(frozen=True, slots=True)
class _DslChainNode:
    """Sequential composition containing one or more conditional/fallback nodes."""

    children: tuple[_DslNode, ...]


@dataclass(frozen=True, slots=True)
class _DslGroupNode:
    """Parallel fan-out composition containing conditional/fallback nodes."""

    children: tuple[_DslNode, ...]


@dataclass(frozen=True, slots=True)
class _DslMapNode:
    """Mapped fan-out over a body containing conditional/fallback nodes."""

    body: _DslNode
    max_concurrency: int | None = None
    max_items: int | None = None
    cancel_timeout_seconds: float = 1.0


class _TaskLike(Protocol):
    """Protocol matching Django ``Task`` wrappers exposing ``func`` and ``module_path``."""

    func: Callable[..., Any]
    module_path: str


type _DslNode = (
    WorkflowSignatureProtocol
    | _BranchNode
    | _FallbackNode
    | _DslChainNode
    | _DslGroupNode
    | _DslMapNode
)
type WorkflowLike = (
    WorkflowSignatureProtocol
    | WorkflowExpr
    | WorkflowBuilder
    | _TaskLike
    | Callable[..., Any]
    | str
)


def _is_callable_target(value: Any) -> bool:
    from django.tasks.base import Task

    return callable(value) or isinstance(value, Task)


def _validate_sync_callable(callable_obj: Any, *, role: str) -> None:
    """Reject coroutine, generator, and async-generator callables for synchronous roles."""
    underlying: Any = getattr(callable_obj, "func", callable_obj)
    if (
        inspect.iscoroutinefunction(callable_obj)
        or inspect.isgeneratorfunction(callable_obj)
        or inspect.isasyncgenfunction(callable_obj)
        or inspect.iscoroutinefunction(underlying)
        or inspect.isgeneratorfunction(underlying)
        or inspect.isasyncgenfunction(underlying)
    ):
        raise _definition_error(f"{role} must be a synchronous module-level callable")


def _canonical_callable_path(
    callable_obj: Callable[..., Any] | _TaskLike | str,
    *,
    role: str,
) -> tuple[str, Callable[..., Any] | None]:
    """Validate and extract the canonical dotted import path of a callable."""
    if isinstance(callable_obj, str):
        if "." not in callable_obj:
            raise _definition_error(
                f"Invalid {role} path '{callable_obj}': expected a dotted import path"
            )
        parts = callable_obj.split(".")
        if not all(part.isidentifier() for part in parts):
            raise _definition_error(
                f"Invalid {role} path '{callable_obj}': expected valid Python identifiers"
            )
        return callable_obj, None

    from django.tasks.base import Task

    is_django_task = isinstance(callable_obj, Task)
    underlying: Any = callable_obj.func if is_django_task else callable_obj
    if not callable(underlying):
        raise TypeError(f"{role} must be an importable callable or dotted path string")

    _validate_sync_callable(underlying, role=role)

    if is_django_task:
        module_path = getattr(callable_obj, "module_path", None)
        if isinstance(module_path, str) and "." in module_path:
            return module_path, underlying

    module = getattr(underlying, "__module__", None)
    name = getattr(underlying, "__name__", None)
    qualname = getattr(underlying, "__qualname__", name)
    if not isinstance(module, str) or not module or not isinstance(name, str) or not name:
        raise _definition_error(f"{role} must be an importable module-level callable")
    if isinstance(qualname, str) and "<locals>" in qualname:
        raise _definition_error(f"{role} must be an importable module-level callable")
    if qualname != name:
        raise _definition_error(f"{role} must be a module-level function, not a method")
    return f"{module}.{name}", underlying


def _validate_positive_int_limit(name: str, value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer or None")
    if value < 1:
        raise ValueError(f"{name} must be at least 1")


def _validate_cancel_timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("cancel_timeout_seconds must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0.0:
        raise ValueError("cancel_timeout_seconds must be finite and non-negative")
    return normalized


def _normalize_exception_types(
    exceptions: type[Exception] | tuple[type[Exception], ...],
) -> tuple[type[Exception], ...]:
    raw_tuple: tuple[Any, ...] = (
        (exceptions,) if isinstance(exceptions, type) else tuple(exceptions)
    )
    if not raw_tuple:
        raise ValueError("exceptions must contain at least one Exception subclass")
    normalized: list[type[Exception]] = []
    for entry in raw_tuple:
        if not isinstance(entry, type) or not issubclass(entry, Exception):
            raise TypeError("exceptions must contain only Exception subclasses (not BaseException)")
        if entry not in normalized:
            normalized.append(entry)
    return tuple(normalized)


def _is_coercible(value: Any) -> bool:
    return (
        isinstance(value, (WorkflowExpr, WorkflowBuilder, str))
        or _is_workflow_signature(value)
        or _is_callable_target(value)
    )


def _coerce_node(target: Any) -> _DslNode:
    """Coerce a workflow signature, expression, builder, or callable into a DSL node."""
    if isinstance(target, WorkflowExpr):
        return target.node
    if _is_workflow_signature(target):
        return target
    if isinstance(target, WorkflowBuilder):
        built = target.build()
        if not _is_workflow_signature(built):
            raise TypeError("WorkflowBuilder.build() must return a WorkflowSignature")
        return built
    if isinstance(target, str) or _is_callable_target(target):
        return _primitives().step_fn(target)
    raise TypeError(
        "Workflow DSL target must be a WorkflowSignature, WorkflowExpr, "
        "WorkflowBuilder, callable, or dotted import path"
    )


def _flatten_chain_nodes(left: _DslNode, right: _DslNode) -> _DslNode:
    left_parts: tuple[_DslNode, ...] = (
        left.signatures
        if _is_chain(left)
        else (left.children if isinstance(left, _DslChainNode) else (left,))
    )
    right_parts: tuple[_DslNode, ...] = (
        right.signatures
        if _is_chain(right)
        else (right.children if isinstance(right, _DslChainNode) else (right,))
    )
    combined = (*left_parts, *right_parts)
    if all(_is_workflow_signature(item) for item in combined):
        return _primitives().chain_fn(*combined)
    return _DslChainNode(children=combined)


def _flatten_group_nodes(left: _DslNode, right: _DslNode) -> _DslNode:
    left_parts: tuple[_DslNode, ...] = (
        left.signatures
        if _is_group(left)
        else (left.children if isinstance(left, _DslGroupNode) else (left,))
    )
    right_parts: tuple[_DslNode, ...] = (
        right.signatures
        if _is_group(right)
        else (right.children if isinstance(right, _DslGroupNode) else (right,))
    )
    combined = (*left_parts, *right_parts)
    if all(_is_workflow_signature(item) for item in combined):
        return _primitives().group_fn(*combined)
    return _DslGroupNode(children=combined)


class _DslCancelledError(Exception):
    """Internal sentinel aborting sibling DSL branches after fail-fast."""


class _DslCancellationScope:
    """Thread-safe fail-fast cancellation state shared by concurrent DSL threads."""

    __slots__ = ("_cancelled", "_lock", "_parent", "_refs")

    def __init__(self, parent: _DslCancellationScope | None = None) -> None:
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._parent = parent
        self._refs: list[Any] = []

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set() or (self._parent is not None and self._parent.is_cancelled)

    def cancel(self) -> None:
        self._cancelled.set()

    def check_cancelled(self) -> None:
        if self.is_cancelled:
            raise _DslCancelledError("Sibling DSL branch cancelled after fail-fast failure")

    def register_refs(self, refs: Sequence[Any]) -> None:
        if not refs:
            return
        with self._lock:
            self._refs.extend(refs)
        if self._parent is not None:
            self._parent.register_refs(refs)

    def snapshot_refs(self) -> list[Any]:
        with self._lock:
            return list(self._refs)


def _is_non_fallback_control_error(error: BaseException) -> bool:
    """Return True for control-plane/fencing errors that fallbacks must never swallow."""
    from django_ray.execution_codec import NestedExecutionRequestRejected
    from django_ray.workflow.plans import WorkflowPlanMismatchError

    return isinstance(
        error,
        (_DslCancelledError, NestedExecutionRequestRejected, WorkflowPlanMismatchError),
    )


def _match_fallback_exception(
    error: Exception,
    exception_types: tuple[type[Exception], ...],
) -> Exception | None:
    """Return ``error`` or an unwrapped ``RayTaskError`` cause matching ``exception_types``."""
    if _is_non_fallback_control_error(error):
        return None
    if type(error).__name__ == "RayTaskError":
        cause = getattr(error, "cause", None)
        if isinstance(cause, BaseException) and _is_non_fallback_control_error(cause):
            return None
        dunder_cause = getattr(error, "__cause__", None)
        if isinstance(dunder_cause, BaseException) and _is_non_fallback_control_error(dunder_cause):
            return None
    if isinstance(error, exception_types):
        return error
    if type(error).__name__ == "RayTaskError":
        cause = getattr(error, "cause", None)
        if isinstance(cause, Exception) and isinstance(cause, exception_types):
            return cause
        dunder_cause = getattr(error, "__cause__", None)
        if isinstance(dunder_cause, Exception) and isinstance(dunder_cause, exception_types):
            return dunder_cause
    return None


def _can_submit_directly_on_executor(node: _DslNode) -> bool:
    """Return whether ``node`` lowers to a direct non-blocking ``_submit`` call."""
    if _is_workflow_signature(node):
        return True
    if isinstance(node, _BranchNode):
        return _can_submit_directly_on_executor(node.on_true) and _can_submit_directly_on_executor(
            node.on_false
        )
    return False


def _resolve_branch_target(
    node: _BranchNode,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> _DslNode:
    predicate = resolve_workflow_callable(node.predicate_path)
    _validate_sync_callable(predicate, role="branch predicate")
    decision = predicate(*args, **kwargs)
    if (
        inspect.isawaitable(decision)
        or inspect.isgenerator(decision)
        or inspect.isasyncgen(decision)
    ):
        close = getattr(decision, "close", None)
        if callable(close):
            close()
        raise _definition_error(
            "branch predicate must return a concrete synchronous value, "
            "not an awaitable or generator"
        )
    return node.on_true if bool(decision) else node.on_false


def _submit_direct_on_executor(
    node: _DslNode,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    executor: Any,
    node_id: str,
    dependencies: tuple[str, ...],
) -> Any:
    """Submit a signature or resolved branch onto a shared executor without blocking."""
    current: _DslNode = node
    while isinstance(current, _BranchNode):
        current = _resolve_branch_target(current, args, kwargs)
    if not _is_workflow_signature(current):
        raise TypeError("Expected a WorkflowSignature after resolving direct DSL branch")
    return current._submit(executor, args, kwargs, node_id, dependencies)


def _cancel_executor_refs(
    executor: Any,
    refs: Sequence[Any],
    *,
    timeout_seconds: float,
) -> None:
    if not refs:
        return
    unresolved_fn = getattr(executor, "_unresolved_cleanup_refs", None)
    targets = (
        unresolved_fn(list(reversed(refs))) if callable(unresolved_fn) else list(reversed(refs))
    )
    if not targets:
        return
    cancel_fn = getattr(executor, "cancel_and_drain_fold_payloads", None)
    if callable(cancel_fn):
        try:
            cancel_fn(targets, timeout_seconds=timeout_seconds)
        except BaseException:
            pass


def _execute_signature_on_executor(
    signature: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    executor: Any,
    node_id: str,
    cancel_timeout_seconds: float = 1.0,
    cancel_scope: _DslCancellationScope | None = None,
) -> Any:
    """Run one WorkflowSignature directly on a shared executor without re-pinning plans."""
    if cancel_scope is not None:
        cancel_scope.check_cancelled()
    cleanup_refs: list[Any] = []
    tracked_refs: list[Any] = cleanup_refs
    capture_cleanup = getattr(executor, "capture_cleanup", None)
    try:
        if callable(capture_cleanup):
            with capture_cleanup(cleanup_refs):
                submission = signature._submit(executor, args, kwargs, node_id, ())
        else:
            submission = signature._submit(executor, args, kwargs, node_id, ())
        tracked_refs = list(cleanup_refs)
        if submission.value is not None and not any(
            existing is submission.value for existing in tracked_refs
        ):
            tracked_refs.append(submission.value)
        if cancel_scope is not None:
            cancel_scope.register_refs(tracked_refs)
            if cancel_scope.is_cancelled:
                _cancel_executor_refs(
                    executor,
                    tracked_refs,
                    timeout_seconds=cancel_timeout_seconds,
                )
                raise _DslCancelledError("Sibling DSL branch cancelled after fail-fast failure")
        return executor.resolve(submission.value)
    except BaseException:
        _cancel_executor_refs(executor, tracked_refs, timeout_seconds=cancel_timeout_seconds)
        raise


def _execute_dsl_group_concurrent(
    node: _DslGroupNode,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    executor: Any,
    node_id: str,
    cancel_timeout_seconds: float = 1.0,
    cancel_scope: _DslCancellationScope | None = None,
) -> list[Any]:
    """Execute a conditional Group concurrently on Ray while preserving fail-fast cleanup."""
    if all(_can_submit_directly_on_executor(child) for child in node.children):
        submissions: list[Any] = []
        branch_cleanup: list[Any] = []
        capture_cleanup = getattr(executor, "capture_cleanup", None)
        try:
            for index, child in enumerate(node.children):
                if cancel_scope is not None:
                    cancel_scope.check_cancelled()
                child_refs: list[Any] = []
                try:
                    if callable(capture_cleanup):
                        with capture_cleanup(child_refs):
                            sub = _submit_direct_on_executor(
                                child,
                                args,
                                kwargs,
                                executor=executor,
                                node_id=f"{node_id}.g{index}",
                                dependencies=(),
                            )
                    else:
                        sub = _submit_direct_on_executor(
                            child,
                            args,
                            kwargs,
                            executor=executor,
                            node_id=f"{node_id}.g{index}",
                            dependencies=(),
                        )
                finally:
                    branch_cleanup.extend(child_refs)
                    if cancel_scope is not None:
                        cancel_scope.register_refs(child_refs)
                submissions.append(sub)
                if cancel_scope is not None:
                    cancel_scope.check_cancelled()
            collected = executor.collect([sub.value for sub in submissions])
            branch_cleanup.append(collected)
            if cancel_scope is not None:
                cancel_scope.register_refs((collected,))
                cancel_scope.check_cancelled()
            return list(executor.resolve(collected))
        except BaseException:
            _cancel_executor_refs(
                executor,
                branch_cleanup,
                timeout_seconds=cancel_timeout_seconds,
            )
            raise

    results: list[Any] = [None] * len(node.children)
    child_scope = _DslCancellationScope(parent=cancel_scope)
    pool = ThreadPoolExecutor(max_workers=max(1, len(node.children)))
    future_to_index: dict[Future[Any], int] = {}
    try:
        for index, child in enumerate(node.children):
            child_scope.check_cancelled()
            ctx = contextvars.copy_context()
            fut = pool.submit(
                ctx.run,
                _execute_node_on_executor,
                child,
                args,
                kwargs,
                executor=executor,
                node_id=f"{node_id}.g{index}",
                cancel_timeout_seconds=cancel_timeout_seconds,
                cancel_scope=child_scope,
            )
            future_to_index[fut] = index
        pending = set(future_to_index)
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for finished in done:
                idx = future_to_index[finished]
                results[idx] = finished.result()
    except BaseException:
        child_scope.cancel()
        for fut in future_to_index:
            fut.cancel()
        _cancel_executor_refs(
            executor,
            child_scope.snapshot_refs(),
            timeout_seconds=cancel_timeout_seconds,
        )
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True)
    return results


def _execute_dsl_map_concurrent(
    node: _DslMapNode,
    items: Iterable[Any],
    *,
    executor: Any,
    node_id: str,
    cancel_scope: _DslCancellationScope | None = None,
) -> list[Any]:
    """Execute a conditional Map concurrently on Ray honoring max_concurrency & cancel timeout."""
    iterator = iter(items)
    if _can_submit_directly_on_executor(node.body):
        pending: list[tuple[int, Any, tuple[Any, ...]]] = []
        ordered_results: list[Any] = []
        submitted = 0
        input_exhausted = False
        admitting_cleanup: list[Any] | None = None
        resolving_cleanup: tuple[Any, ...] = ()
        capture_cleanup = getattr(executor, "capture_cleanup", None)
        try:
            while pending or not input_exhausted:
                while not input_exhausted and (
                    node.max_concurrency is None or len(pending) < node.max_concurrency
                ):
                    if cancel_scope is not None:
                        cancel_scope.check_cancelled()
                    try:
                        item = next(iterator)
                    except StopIteration:
                        input_exhausted = True
                        break
                    if node.max_items is not None and submitted >= node.max_items:
                        raise _definition_error(f"map input exceeds max_items={node.max_items}")
                    admitting_cleanup = []
                    try:
                        if callable(capture_cleanup):
                            with capture_cleanup(admitting_cleanup):
                                sub = _submit_direct_on_executor(
                                    node.body,
                                    (item,),
                                    {},
                                    executor=executor,
                                    node_id=f"{node_id}.m{submitted}",
                                    dependencies=(),
                                )
                        else:
                            sub = _submit_direct_on_executor(
                                node.body,
                                (item,),
                                {},
                                executor=executor,
                                node_id=f"{node_id}.m{submitted}",
                                dependencies=(),
                            )
                    finally:
                        if cancel_scope is not None:
                            cancel_scope.register_refs(admitting_cleanup)
                    ordered_results.append(None)
                    pending.append((submitted, sub, tuple(reversed(admitting_cleanup))))
                    admitting_cleanup = None
                    submitted += 1
                    if cancel_scope is not None:
                        cancel_scope.check_cancelled()
                if not pending:
                    continue
                pending_values = [sub.value for _, sub, _ in pending]
                ready_idx = executor.wait_one(pending_values)
                item_index, sub, resolving_cleanup = pending.pop(ready_idx)
                ordered_results[item_index] = executor.resolve_ready(sub.value)
                resolving_cleanup = ()
        except BaseException:
            close_iter = getattr(iterator, "close", None)
            if callable(close_iter):
                try:
                    close_iter()
                except BaseException:
                    pass
            cleanup_refs = list(reversed(admitting_cleanup or ()))
            cleanup_refs.extend(resolving_cleanup)
            cleanup_refs.extend(ref for _, _, refs in pending for ref in refs)
            _cancel_executor_refs(
                executor,
                cleanup_refs,
                timeout_seconds=node.cancel_timeout_seconds,
            )
            raise
        return ordered_results

    max_workers = node.max_concurrency if node.max_concurrency is not None else 32
    child_scope = _DslCancellationScope(parent=cancel_scope)
    pool = ThreadPoolExecutor(max_workers=max(1, max_workers))
    in_flight: dict[Future[Any], int] = {}
    ordered_results = []
    submitted = 0
    input_exhausted = False
    try:
        while in_flight or not input_exhausted:
            while not input_exhausted and (
                node.max_concurrency is None or len(in_flight) < node.max_concurrency
            ):
                child_scope.check_cancelled()
                try:
                    item = next(iterator)
                except StopIteration:
                    input_exhausted = True
                    break
                if node.max_items is not None and submitted >= node.max_items:
                    raise _definition_error(f"map input exceeds max_items={node.max_items}")
                ordered_results.append(None)
                ctx = contextvars.copy_context()
                fut = pool.submit(
                    ctx.run,
                    _execute_node_on_executor,
                    node.body,
                    (item,),
                    {},
                    executor=executor,
                    node_id=f"{node_id}.m{submitted}",
                    cancel_timeout_seconds=node.cancel_timeout_seconds,
                    cancel_scope=child_scope,
                )
                in_flight[fut] = submitted
                submitted += 1
            if not in_flight:
                continue
            done, _ = wait(set(in_flight), return_when=FIRST_COMPLETED)
            for finished in done:
                idx = in_flight.pop(finished)
                ordered_results[idx] = finished.result()
    except BaseException:
        child_scope.cancel()
        close_iter = getattr(iterator, "close", None)
        if callable(close_iter):
            try:
                close_iter()
            except BaseException:
                pass
        for fut in in_flight:
            fut.cancel()
        _cancel_executor_refs(
            executor,
            child_scope.snapshot_refs(),
            timeout_seconds=node.cancel_timeout_seconds,
        )
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True)
    return ordered_results


def _execute_node_on_executor(
    node: _DslNode,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    executor: Any,
    node_id: str = "0",
    cancel_timeout_seconds: float = 1.0,
    cancel_scope: _DslCancellationScope | None = None,
) -> Any:
    """Execute a DSL AST node using a single cohesive shared executor."""
    if cancel_scope is not None:
        cancel_scope.check_cancelled()
    if _is_workflow_signature(node):
        return _execute_signature_on_executor(
            node,
            args,
            kwargs,
            executor=executor,
            node_id=node_id,
            cancel_timeout_seconds=cancel_timeout_seconds,
            cancel_scope=cancel_scope,
        )

    if isinstance(node, _DslChainNode):
        current: Any = None
        for index, child in enumerate(node.children):
            if cancel_scope is not None:
                cancel_scope.check_cancelled()
            if index == 0:
                current = _execute_node_on_executor(
                    child,
                    args,
                    kwargs,
                    executor=executor,
                    node_id=f"{node_id}.{index}",
                    cancel_timeout_seconds=cancel_timeout_seconds,
                    cancel_scope=cancel_scope,
                )
            else:
                current = _execute_node_on_executor(
                    child,
                    (current,),
                    {},
                    executor=executor,
                    node_id=f"{node_id}.{index}",
                    cancel_timeout_seconds=cancel_timeout_seconds,
                    cancel_scope=cancel_scope,
                )
        return current

    if isinstance(node, _DslGroupNode):
        if not is_local_workflow_executor(executor) and len(node.children) > 1:
            return _execute_dsl_group_concurrent(
                node,
                args,
                kwargs,
                executor=executor,
                node_id=node_id,
                cancel_timeout_seconds=cancel_timeout_seconds,
                cancel_scope=cancel_scope,
            )
        return [
            _execute_node_on_executor(
                child,
                args,
                kwargs,
                executor=executor,
                node_id=f"{node_id}.g{index}",
                cancel_timeout_seconds=cancel_timeout_seconds,
                cancel_scope=cancel_scope,
            )
            for index, child in enumerate(node.children)
        ]

    if isinstance(node, _DslMapNode):
        if len(args) != 1 or kwargs:
            raise _definition_error("map expects exactly one iterable input")
        items = args[0]
        if isinstance(items, (str, bytes, dict)) or not isinstance(items, Iterable):
            raise _definition_error("map input must be a non-string iterable")
        if node.max_items is not None and isinstance(items, Sized) and len(items) > node.max_items:
            raise _definition_error(f"map input exceeds max_items={node.max_items}")
        if not is_local_workflow_executor(executor):
            return _execute_dsl_map_concurrent(
                node,
                items,
                executor=executor,
                node_id=node_id,
                cancel_scope=cancel_scope,
            )
        results: list[Any] = []
        for index, item in enumerate(items):
            if node.max_items is not None and index >= node.max_items:
                raise _definition_error(f"map input exceeds max_items={node.max_items}")
            results.append(
                _execute_node_on_executor(
                    node.body,
                    (item,),
                    {},
                    executor=executor,
                    node_id=f"{node_id}.m{index}",
                    cancel_timeout_seconds=node.cancel_timeout_seconds,
                    cancel_scope=cancel_scope,
                )
            )
        return results

    if isinstance(node, _BranchNode):
        selected = _resolve_branch_target(node, args, kwargs)
        return _execute_node_on_executor(
            selected,
            args,
            kwargs,
            executor=executor,
            node_id=f"{node_id}.branch",
            cancel_timeout_seconds=cancel_timeout_seconds,
            cancel_scope=cancel_scope,
        )

    assert isinstance(node, _FallbackNode)
    try:
        return _execute_node_on_executor(
            node.primary,
            args,
            kwargs,
            executor=executor,
            node_id=f"{node_id}.primary",
            cancel_timeout_seconds=cancel_timeout_seconds,
            cancel_scope=cancel_scope,
        )
    except Exception as error:
        if cancel_scope is not None:
            cancel_scope.check_cancelled()
        matched = _match_fallback_exception(error, node.exception_types)
        if matched is None:
            raise
        if node.pass_error:
            return _execute_node_on_executor(
                node.fallback,
                (*args, matched),
                kwargs,
                executor=executor,
                node_id=f"{node_id}.fallback",
                cancel_timeout_seconds=cancel_timeout_seconds,
                cancel_scope=cancel_scope,
            )
        return _execute_node_on_executor(
            node.fallback,
            args,
            kwargs,
            executor=executor,
            node_id=f"{node_id}.fallback",
            cancel_timeout_seconds=cancel_timeout_seconds,
            cancel_scope=cancel_scope,
        )


def _execute_node(
    node: _DslNode,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    use_ray: bool | None,
) -> Any:
    """Execute a DSL AST node cleanly while preserving native signature paths."""
    if _is_workflow_signature(node):
        return cast(Any, node).run(*args, use_ray=use_ray, **kwargs)

    # Run conditional/fallback DSL expressions on one cohesive executor so
    # sub-steps do not fragment durable progress runs or re-pin conflicting plans.
    executor = get_workflow_executor(use_ray)
    bind_dynamic_run = getattr(executor, "bind_dynamic_run", None)
    if callable(bind_dynamic_run):
        bind_dynamic_run()
    finish_progress = getattr(executor, "finish_progress", None)
    failed = False
    try:
        return _execute_node_on_executor(node, args, kwargs, executor=executor, node_id="0")
    except BaseException:
        failed = True
        raise
    finally:
        if callable(finish_progress):
            finish_progress(failed=failed)


class WorkflowExpr:
    """Immutable, composable Workflow DSL expression wrapper.

    Supports ``|`` (sequential pipe -> ``Chain``), ``&`` (parallel fan-out ->
    ``Group``), ``.map(...)``, ``.with_fallback(...)``, ``.explain(...)``,
    ``.build()``, and ``.run(...)``.
    """

    __slots__ = ("_node",)

    _node: _DslNode

    def __init__(self, target: WorkflowLike) -> None:
        node = _coerce_node(target)
        object.__setattr__(self, "_node", node)

    @classmethod
    def _from_node(cls, node: _DslNode) -> WorkflowExpr:
        instance = cls.__new__(cls)
        object.__setattr__(instance, "_node", node)
        return instance

    @property
    def node(self) -> _DslNode:
        """Return the underlying immutable workflow signature or DSL node."""
        return self._node

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("WorkflowExpr instances are immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("WorkflowExpr instances are immutable")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, WorkflowExpr):
            return NotImplemented
        return self._node == other._node

    def __repr__(self) -> str:
        return f"WorkflowExpr(kind={_node_kind(self._node)!r})"

    def __or__(self, other: Any) -> WorkflowExpr:
        if not _is_coercible(other):
            return NotImplemented
        return WorkflowExpr._from_node(_flatten_chain_nodes(self._node, _coerce_node(other)))

    def __ror__(self, other: Any) -> WorkflowExpr:
        if not _is_coercible(other):
            return NotImplemented
        return WorkflowExpr._from_node(_flatten_chain_nodes(_coerce_node(other), self._node))

    def __and__(self, other: Any) -> WorkflowExpr:
        if not _is_coercible(other):
            return NotImplemented
        return WorkflowExpr._from_node(_flatten_group_nodes(self._node, _coerce_node(other)))

    def __rand__(self, other: Any) -> WorkflowExpr:
        if not _is_coercible(other):
            return NotImplemented
        return WorkflowExpr._from_node(_flatten_group_nodes(_coerce_node(other), self._node))

    def map(
        self,
        target: WorkflowLike,
        *,
        max_concurrency: int | None = None,
        max_items: int | None = None,
        cancel_timeout_seconds: float = 1.0,
        django: bool = False,
        ray_options: dict[str, Any] | None = None,
        runtime_env: str | dict[str, Any] | None = None,
    ) -> WorkflowExpr:
        """Pipe a ``Map`` stage over the iterable produced by this expression."""
        _validate_positive_int_limit("max_concurrency", max_concurrency)
        _validate_positive_int_limit("max_items", max_items)
        normalized_timeout = _validate_cancel_timeout(cancel_timeout_seconds)

        primitives = _primitives()
        if isinstance(target, WorkflowExpr):
            if django or ray_options or runtime_env is not None:
                raise _definition_error(
                    "Options cannot be added when mapping an existing WorkflowExpr"
                )
            body_node = target.node
        elif _is_workflow_signature(target):
            body_node = primitives.map_step_fn(
                target,
                django=django,
                ray_options=ray_options,
                runtime_env=runtime_env,
            ).signature
        elif isinstance(target, WorkflowBuilder):
            if django or ray_options or runtime_env is not None:
                raise _definition_error(
                    "Options cannot be added when mapping an existing WorkflowBuilder"
                )
            body_node = _coerce_node(target)
        elif isinstance(target, str) or _is_callable_target(target):
            target_callable: Any = target
            body_node = primitives.step_fn(
                target_callable,
                django=django,
                ray_options=ray_options,
                runtime_env=runtime_env,
            )
        else:
            raise TypeError(
                "map target must be a WorkflowSignature, WorkflowExpr, "
                "WorkflowBuilder, callable, or dotted import path"
            )

        if _is_workflow_signature(body_node):
            mapped: _DslNode = primitives.map_cls(
                body_node,
                max_concurrency=max_concurrency,
                max_items=max_items,
                cancel_timeout_seconds=normalized_timeout,
            )
        else:
            mapped = _DslMapNode(
                body=body_node,
                max_concurrency=max_concurrency,
                max_items=max_items,
                cancel_timeout_seconds=normalized_timeout,
            )
        return WorkflowExpr._from_node(_flatten_chain_nodes(self._node, mapped))

    def with_fallback(
        self,
        fallback_fn: WorkflowLike,
        *,
        exceptions: type[Exception] | tuple[type[Exception], ...] = (Exception,),
        pass_error: bool = False,
    ) -> WorkflowExpr:
        """Attach an explicit fallback step or workflow invoked on matching failures."""
        if type(pass_error) is not bool:
            raise TypeError("pass_error must be a boolean")
        normalized_exceptions = _normalize_exception_types(exceptions)
        fallback_node = _coerce_node(fallback_fn)
        return WorkflowExpr._from_node(
            _FallbackNode(
                primary=self._node,
                fallback=fallback_node,
                exception_types=normalized_exceptions,
                pass_error=pass_error,
            )
        )

    def build(self) -> Any:
        """Materialize a standard ``WorkflowSignature`` (``Step``/``Chain``/``Group``/``Map``)."""
        if _is_workflow_signature(self._node):
            return self._node
        raise _definition_error(
            "Cannot build a static WorkflowSignature from an expression containing "
            "branch or with_fallback nodes; execute via .run() or inspect via .explain()"
        )

    def run(
        self,
        *args: Any,
        use_ray: bool | None = None,
        **kwargs: Any,
    ) -> Any:
        """Execute this workflow expression locally or on Ray."""
        return _execute_node(self._node, args, kwargs, use_ray=use_ray)

    @overload
    def explain(
        self,
        *,
        format: Literal["text"] = "text",  # noqa: A002
        build_context: WorkflowPlanBuildContext | None = None,
        invocation_args: Sequence[Any] = (),
        invocation_kwargs: Mapping[str, Any] | None = None,
    ) -> str: ...

    @overload
    def explain(
        self,
        *,
        format: Literal["dict"],  # noqa: A002
        build_context: WorkflowPlanBuildContext | None = None,
        invocation_args: Sequence[Any] = (),
        invocation_kwargs: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def explain(
        self,
        *,
        format: Literal["text", "dict"] = "text",  # noqa: A002
        build_context: WorkflowPlanBuildContext | None = None,
        invocation_args: Sequence[Any] = (),
        invocation_kwargs: Mapping[str, Any] | None = None,
    ) -> str | dict[str, Any]:
        """Produce a secret-free dry-run explanation of this workflow expression."""
        return explain_workflow(
            self,
            format=format,
            build_context=build_context,
            invocation_args=invocation_args,
            invocation_kwargs=invocation_kwargs,
        )


def pipe(*steps: WorkflowLike) -> WorkflowExpr:
    """Compose one or more steps sequentially into a ``WorkflowExpr``."""
    if not steps:
        raise _definition_error("pipe requires at least one step")
    current = WorkflowExpr(steps[0])
    for item in steps[1:]:
        current = current | item
    return current


def parallel(*steps: WorkflowLike) -> WorkflowExpr:
    """Compose one or more steps as a parallel ``Group`` fan-out."""
    if not steps:
        raise _definition_error("parallel requires at least one step")
    nodes = tuple(_coerce_node(item) for item in steps)
    if all(_is_workflow_signature(node) for node in nodes):
        return WorkflowExpr._from_node(_primitives().group_fn(*nodes))
    return WorkflowExpr._from_node(_DslGroupNode(children=nodes))


def branch(
    predicate: Callable[..., Any] | _TaskLike | str,
    *,
    on_true: WorkflowLike,
    on_false: WorkflowLike,
) -> WorkflowExpr:
    """Create a declarative conditional routing step with canonical callable identity."""
    predicate_path, predicate_callable = _canonical_callable_path(
        predicate,
        role="branch predicate",
    )
    true_node = _coerce_node(on_true)
    false_node = _coerce_node(on_false)
    return WorkflowExpr._from_node(
        _BranchNode(
            predicate_path=predicate_path,
            on_true=true_node,
            on_false=false_node,
            predicate_callable=predicate_callable,
        )
    )


def _node_kind(node: _DslNode) -> str:
    if _is_step(node):
        return "step"
    if _is_chain(node) or isinstance(node, _DslChainNode):
        return "chain"
    if _is_group(node) or isinstance(node, _DslGroupNode):
        return "group"
    if _is_map(node) or isinstance(node, _DslMapNode):
        return "map"
    if isinstance(node, _BranchNode):
        return "branch"
    return "fallback"


def _safe_scalar_or_mapping(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): item
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if isinstance(item, (bool, int, float, str)) or item is None
        }
    return type(value).__name__


def _safe_ray_options(options: Mapping[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for key in sorted(str(k) for k in options):
        if key in _SAFE_RAY_OPTION_KEYS:
            projected[key] = _safe_scalar_or_mapping(options[key])
    return projected


def _safe_runtime_env_summary(
    runtime_env: str | Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return a strictly secret-free RuntimeEnv summary (never raw values)."""
    if runtime_env is None:
        return {"mode": "inherit", "profile": None}
    if isinstance(runtime_env, str):
        return {"mode": "profile", "profile": runtime_env}
    return {
        "mode": "inline",
        "profile": None,
        "keys": sorted(str(key) for key in runtime_env),
    }


def _safe_map_transport_summary(node: Any) -> dict[str, Any]:
    if _is_map(node) and node.result_buffer is not None:
        return {
            "mode": "result_buffer",
            "max_serialized_bytes": node.result_buffer.max_serialized_bytes,
            "actor_options": _safe_ray_options(node.result_buffer.actor_options),
        }
    if _is_map(node) and node.result_fold is not None:
        return {
            "mode": "result_fold",
            "reducer": node.result_fold.reducer.callable_path,
            "max_serialized_bytes": node.result_fold.max_serialized_bytes,
            "actor_options": _safe_ray_options(node.result_fold.actor_options),
        }
    return {"mode": "default"}


def _exception_type_name(exc_type: type[Exception]) -> str:
    if exc_type.__module__ == "builtins":
        return exc_type.__qualname__
    return f"{exc_type.__module__}.{exc_type.__qualname__}"


@dataclass
class _ExplainCollector:
    nodes: list[dict[str, Any]]
    runtime_env_profiles: set[str]
    step_count: int = 0
    group_count: int = 0
    map_count: int = 0
    branch_count: int = 0
    fallback_count: int = 0
    has_unbounded_map: bool = False
    has_django_steps: bool = False

    def visit(self, node: _DslNode, node_id: str, depth: int) -> dict[str, Any]:
        if _is_step(node):
            step_node: Any = node
            self.step_count += 1
            if step_node.bootstrap_django:
                self.has_django_steps = True
            env_summary = _safe_runtime_env_summary(step_node.runtime_env)
            profile = env_summary.get("profile")
            if isinstance(profile, str):
                self.runtime_env_profiles.add(profile)
            entry: dict[str, Any] = {
                "node_id": node_id,
                "kind": "step",
                "depth": depth,
                "callable_path": step_node.callable_path,
                "django": step_node.bootstrap_django,
                "runtime_env": env_summary,
                "ray_options": _safe_ray_options(step_node.ray_options),
                "bound_arg_count": len(step_node.bound_args),
                "bound_kwarg_names": sorted(str(key) for key in step_node.bound_kwargs),
                "output_preview": step_node.output_preview_path,
                "limits": None,
                "result_transport": None,
                "branch": None,
                "fallback": None,
                "children": [],
            }
            self.nodes.append(entry)
            return {**entry, "child_trees": []}

        if _is_chain(node) or isinstance(node, _DslChainNode):
            chain_node: Any = node
            children_nodes: tuple[_DslNode, ...] = (
                chain_node.signatures if _is_chain(node) else chain_node.children
            )
            child_ids = [f"{node_id}.{idx}" for idx in range(len(children_nodes))]
            entry = {
                "node_id": node_id,
                "kind": "chain",
                "depth": depth,
                "callable_path": None,
                "django": None,
                "runtime_env": None,
                "ray_options": {},
                "bound_arg_count": 0,
                "bound_kwarg_names": [],
                "output_preview": None,
                "limits": None,
                "result_transport": None,
                "branch": None,
                "fallback": None,
                "children": child_ids,
            }
            self.nodes.append(entry)
            child_trees = [
                self.visit(child, child_id, depth + 1)
                for child, child_id in zip(children_nodes, child_ids, strict=True)
            ]
            return {**entry, "child_trees": child_trees}

        if _is_group(node) or isinstance(node, _DslGroupNode):
            group_node: Any = node
            self.group_count += 1
            children_nodes = group_node.signatures if _is_group(node) else group_node.children
            child_ids = [f"{node_id}.g{idx}" for idx in range(len(children_nodes))]
            entry = {
                "node_id": node_id,
                "kind": "group",
                "depth": depth,
                "callable_path": None,
                "django": None,
                "runtime_env": None,
                "ray_options": {},
                "bound_arg_count": 0,
                "bound_kwarg_names": [],
                "output_preview": None,
                "limits": None,
                "result_transport": None,
                "branch": None,
                "fallback": None,
                "children": child_ids,
            }
            self.nodes.append(entry)
            child_trees = [
                self.visit(child, child_id, depth + 1)
                for child, child_id in zip(children_nodes, child_ids, strict=True)
            ]
            return {**entry, "child_trees": child_trees}

        if _is_map(node) or isinstance(node, _DslMapNode):
            map_node: Any = node
            self.map_count += 1
            bounded = map_node.max_concurrency is not None and map_node.max_items is not None
            if not bounded:
                self.has_unbounded_map = True
            body: _DslNode = map_node.signature if _is_map(node) else map_node.body
            body_id = f"{node_id}.m*"
            entry = {
                "node_id": node_id,
                "kind": "map",
                "depth": depth,
                "callable_path": None,
                "django": None,
                "runtime_env": None,
                "ray_options": {},
                "bound_arg_count": 0,
                "bound_kwarg_names": [],
                "output_preview": None,
                "limits": {
                    "max_concurrency": map_node.max_concurrency,
                    "max_items": map_node.max_items,
                    "cancel_timeout_seconds": map_node.cancel_timeout_seconds,
                    "bounded": bounded,
                },
                "result_transport": _safe_map_transport_summary(node),
                "branch": None,
                "fallback": None,
                "children": [body_id],
            }
            self.nodes.append(entry)
            body_tree = self.visit(body, body_id, depth + 1)
            return {**entry, "child_trees": [body_tree]}

        if isinstance(node, _BranchNode):
            self.branch_count += 1
            true_id = f"{node_id}.true"
            false_id = f"{node_id}.false"
            entry = {
                "node_id": node_id,
                "kind": "branch",
                "depth": depth,
                "callable_path": node.predicate_path,
                "django": None,
                "runtime_env": None,
                "ray_options": {},
                "bound_arg_count": 0,
                "bound_kwarg_names": [],
                "output_preview": None,
                "limits": None,
                "result_transport": None,
                "branch": {
                    "predicate": node.predicate_path,
                    "on_true": true_id,
                    "on_false": false_id,
                },
                "fallback": None,
                "children": [true_id, false_id],
            }
            self.nodes.append(entry)
            child_trees = [
                self.visit(node.on_true, true_id, depth + 1),
                self.visit(node.on_false, false_id, depth + 1),
            ]
            return {**entry, "child_trees": child_trees}

        if isinstance(node, _FallbackNode):
            self.fallback_count += 1
            primary_id = f"{node_id}.primary"
            fallback_id = f"{node_id}.fallback"
            entry = {
                "node_id": node_id,
                "kind": "fallback",
                "depth": depth,
                "callable_path": None,
                "django": None,
                "runtime_env": None,
                "ray_options": {},
                "bound_arg_count": 0,
                "bound_kwarg_names": [],
                "output_preview": None,
                "limits": None,
                "result_transport": None,
                "branch": None,
                "fallback": {
                    "exceptions": [_exception_type_name(exc) for exc in node.exception_types],
                    "pass_error": node.pass_error,
                    "primary": primary_id,
                    "fallback": fallback_id,
                },
                "children": [primary_id, fallback_id],
            }
            self.nodes.append(entry)
            child_trees = [
                self.visit(node.primary, primary_id, depth + 1),
                self.visit(node.fallback, fallback_id, depth + 1),
            ]
            return {**entry, "child_trees": child_trees}

        raise TypeError(f"Unsupported workflow node type: {type(node).__name__}")


def _inspect_plan_metadata(
    node: _DslNode,
    *,
    build_context: WorkflowPlanBuildContext | None,
    invocation_args: Sequence[Any],
    invocation_kwargs: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], bool | None]:
    if not _is_workflow_signature(node):
        return (
            {
                "fingerprint": None,
                "definition_name": None,
                "definition_revision": None,
                "plan_format": None,
                "plan_format_version": None,
                "retry_safe": None,
                "retry_unsafe_paths": [],
                "eligible_strategies": [],
                "unavailable_reason": "conditional_dsl_expression",
            },
            None,
        )

    try:
        materialized = materialize_workflow_plan(
            cast(PlannableWorkflowSignature, node),
            invocation_args=invocation_args,
            invocation_kwargs=invocation_kwargs,
            build_context=build_context,
        )
    except (WorkflowPlanValidationError, ImproperlyConfigured) as error:
        redacted = redact_text(str(error))[:_EXPLAIN_ERROR_MAX_CHARS]
        return (
            {
                "fingerprint": None,
                "definition_name": None,
                "definition_revision": None,
                "plan_format": None,
                "plan_format_version": None,
                "retry_safe": None,
                "retry_unsafe_paths": [],
                "eligible_strategies": [],
                "unavailable_reason": f"{type(error).__name__}: {redacted}",
            },
            None,
        )

    plan = materialized.plan
    summary = plan.summary()
    callables = plan.manifest.get("callables", ())
    all_callables_stable: bool | None = None
    if isinstance(callables, Sequence) and callables:
        all_callables_stable = all(
            isinstance(item, Mapping)
            and isinstance(item.get("code_identity"), Mapping)
            and item["code_identity"].get("stable") is True
            for item in callables
        )
    return (
        {
            "fingerprint": plan.fingerprint,
            "definition_name": summary["definition_name"],
            "definition_revision": summary["definition_revision"],
            "plan_format": summary["plan_format"],
            "plan_format_version": summary["plan_format_version"],
            "retry_safe": plan.retry_safe,
            "retry_unsafe_paths": list(plan.retry_unsafe_paths),
            "eligible_strategies": list(plan.eligibility.eligible_strategies),
            "unavailable_reason": None,
        },
        all_callables_stable,
    )


def _format_node_label(node: Mapping[str, Any]) -> str:
    kind = str(node["kind"])
    node_id = str(node["node_id"])
    if kind == "step":
        parts: list[str] = [f"django={node['django']}"]
        env = node["runtime_env"]
        if env["mode"] == "profile":
            parts.append(f"env=profile:{env['profile']}")
        elif env["mode"] == "inline":
            keys = ",".join(env["keys"]) if env["keys"] else "empty"
            parts.append(f"env=inline({keys})")
        else:
            parts.append("env=inherit")
        ray_options: Mapping[str, Any] = node["ray_options"]
        for opt_key in sorted(ray_options):
            parts.append(f"{opt_key}={ray_options[opt_key]}")
        if node["output_preview"]:
            parts.append(f"preview={node['output_preview']}")
        return f"[{node_id}] step {node['callable_path']} ({', '.join(parts)})"

    if kind == "map":
        limits = node["limits"]
        transport = node["result_transport"]
        parts = [
            f"max_concurrency={limits['max_concurrency']}",
            f"max_items={limits['max_items']}",
        ]
        if transport["mode"] == "result_buffer":
            parts.append(f"transport=result_buffer({transport['max_serialized_bytes']}B)")
        elif transport["mode"] == "result_fold":
            parts.append(
                f"transport=result_fold({transport['reducer']}, "
                f"{transport['max_serialized_bytes']}B)"
            )
        else:
            parts.append("transport=default")
        return f"[{node_id}] map ({', '.join(parts)})"

    if kind == "branch":
        branch_info = node["branch"]
        return f"[{node_id}] branch predicate={branch_info['predicate']}"

    if kind == "fallback":
        fallback_info = node["fallback"]
        excs = ",".join(fallback_info["exceptions"])
        return f"[{node_id}] fallback on=({excs}) pass_error={fallback_info['pass_error']}"

    return f"[{node_id}] {kind}"


def _render_tree_lines(
    tree: Mapping[str, Any], prefix: str = "", is_last: bool = True
) -> list[str]:
    connector = "" if not prefix and tree["depth"] == 0 else ("`-- " if is_last else "|-- ")
    lines = [f"{prefix}{connector}{_format_node_label(tree)}"]
    child_trees: Sequence[Mapping[str, Any]] = tree["child_trees"]
    child_prefix = (
        ""
        if not prefix and tree["depth"] == 0
        else (f"{prefix}    " if is_last else f"{prefix}|   ")
    )
    for index, child in enumerate(child_trees):
        lines.extend(
            _render_tree_lines(
                child,
                prefix=child_prefix + ("  " if tree["depth"] == 0 else ""),
                is_last=index == len(child_trees) - 1,
            )
        )
    return lines


def _render_text_explanation(report: Mapping[str, Any]) -> str:
    summary = report["summary"]
    stability = report["stability"]
    plan = report["plan"]
    profiles = ", ".join(summary["runtime_env_profiles"]) or "none"
    strategies = ", ".join(plan["eligible_strategies"]) or "none"
    retry_safe_text = (
        "yes"
        if plan["retry_safe"] is True
        else ("no" if plan["retry_safe"] is False else "unavailable")
    )
    lines = [
        "Workflow Explain",
        (
            f"  Root: {report['root_kind']} | Topology: {report['topology_class']} "
            f"| Stability: {stability['classification']}"
        ),
        (
            f"  Nodes: {summary['total_nodes']} "
            f"(steps={summary['step_count']}, groups={summary['group_count']}, "
            f"maps={summary['map_count']}, branches={summary['branch_count']}, "
            f"fallbacks={summary['fallback_count']})"
        ),
        f"  RuntimeEnv profiles: {profiles}",
    ]
    if plan["fingerprint"] is not None:
        lines.append(f"  Plan fingerprint: {plan['fingerprint']}")
        lines.append(f"  Retry-safe: {retry_safe_text} | Eligible strategies: {strategies}")
        if plan["retry_unsafe_paths"]:
            lines.append(f"  Retry-unsafe paths: {', '.join(plan['retry_unsafe_paths'])}")
    else:
        lines.append(f"  Plan fingerprint: unavailable ({plan['unavailable_reason']})")
    lines.append("")
    lines.append("Topology Tree:")
    for tree_line in _render_tree_lines(report["tree"]):
        lines.append(f"  {tree_line}")
    return "\n".join(lines)


def _unwrap_explainable(workflow: Any) -> _DslNode:
    if isinstance(workflow, WorkflowExpr):
        return workflow.node
    if _is_workflow_signature(workflow):
        return workflow
    if isinstance(workflow, WorkflowBuilder):
        built = workflow.build()
        if not _is_workflow_signature(built):
            raise TypeError("WorkflowBuilder.build() must return a WorkflowSignature")
        return built
    raise TypeError("workflow must be a WorkflowSignature, WorkflowExpr, or WorkflowBuilder")


@overload
def explain_workflow(
    workflow: WorkflowSignatureProtocol | WorkflowExpr | WorkflowBuilder,
    *,
    format: Literal["text"] = "text",  # noqa: A002
    build_context: WorkflowPlanBuildContext | None = None,
    invocation_args: Sequence[Any] = (),
    invocation_kwargs: Mapping[str, Any] | None = None,
) -> str: ...


@overload
def explain_workflow(
    workflow: WorkflowSignatureProtocol | WorkflowExpr | WorkflowBuilder,
    *,
    format: Literal["dict"],  # noqa: A002
    build_context: WorkflowPlanBuildContext | None = None,
    invocation_args: Sequence[Any] = (),
    invocation_kwargs: Mapping[str, Any] | None = None,
) -> dict[str, Any]: ...


def explain_workflow(
    workflow: WorkflowSignatureProtocol | WorkflowExpr | WorkflowBuilder,
    *,
    format: Literal["text", "dict"] = "text",  # noqa: A002
    build_context: WorkflowPlanBuildContext | None = None,
    invocation_args: Sequence[Any] = (),
    invocation_kwargs: Mapping[str, Any] | None = None,
) -> str | dict[str, Any]:
    """Inspect a workflow dry-run without executing callables or touching Ray/DB.

    Returns a secret-free formatted tree summary (``format="text"``) or
    structured dictionary (``format="dict"``) detailing stage topology,
    callable dotted paths, map bounds, RuntimeEnv profile names, transport
    settings, stability classification, and canonical effective-plan
    fingerprint.
    """
    if format not in {"text", "dict"}:
        raise ValueError("format must be 'text' or 'dict'")

    root_node = _unwrap_explainable(workflow)
    collector = _ExplainCollector(nodes=[], runtime_env_profiles=set())
    tree = collector.visit(root_node, "0", 0)
    plan_info, all_callables_stable = _inspect_plan_metadata(
        root_node,
        build_context=build_context,
        invocation_args=invocation_args,
        invocation_kwargs=invocation_kwargs,
    )

    has_conditional = collector.branch_count > 0 or collector.fallback_count > 0
    if has_conditional:
        topology_class = "conditional"
        stability_class = "conditional"
    elif collector.map_count > 0:
        topology_class = "dynamic"
        stability_class = "dynamic_unbounded" if collector.has_unbounded_map else "dynamic_bounded"
    else:
        topology_class = "static"
        stability_class = "static_deterministic"

    report: dict[str, Any] = {
        "schema": EXPLAIN_SCHEMA,
        "schema_version": EXPLAIN_SCHEMA_VERSION,
        "root_kind": _node_kind(root_node),
        "topology_class": topology_class,
        "stability": {
            "classification": stability_class,
            "retry_safe": plan_info["retry_safe"],
            "eligible_strategies": list(plan_info["eligible_strategies"]),
            "all_callables_stable": all_callables_stable,
        },
        "summary": {
            "total_nodes": len(collector.nodes),
            "step_count": collector.step_count,
            "group_count": collector.group_count,
            "map_count": collector.map_count,
            "branch_count": collector.branch_count,
            "fallback_count": collector.fallback_count,
            "has_unbounded_map": collector.has_unbounded_map,
            "has_django_steps": collector.has_django_steps,
            "runtime_env_profiles": sorted(collector.runtime_env_profiles),
        },
        "plan": plan_info,
        "nodes": collector.nodes,
        "tree": tree,
    }
    if format == "dict":
        return report
    return _render_text_explanation(report)


__all__ = [
    "EXPLAIN_SCHEMA",
    "EXPLAIN_SCHEMA_VERSION",
    "WorkflowBuilder",
    "WorkflowExpr",
    "WorkflowLike",
    "branch",
    "explain_workflow",
    "parallel",
    "pipe",
]
