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
from collections.abc import Callable, Iterable, Iterator, Sequence, Sized
from contextlib import AbstractContextManager, contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.runtime.import_utils import import_callable


class WorkflowDefinitionError(ValueError):
    """Raised when a workflow signature cannot be constructed or executed."""


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
    return json.loads(json.dumps(value, default=str))


def _clone_runtime_env(
    runtime_env: str | dict[str, Any] | None,
) -> str | dict[str, Any] | None:
    if isinstance(runtime_env, dict):
        return deepcopy(runtime_env)
    return runtime_env


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


class _RayExecutor(_Executor):
    def __init__(self) -> None:
        import ray

        from django_ray.runtime.context import get_current_task_context

        self.ray = ray

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
        self.last_progress_flush_at = time.monotonic()
        self._progress_suppression_depth = 0
        self._map_progress_sent_at: dict[str, float] = {}
        if self.task_context is not None:
            identity = WorkflowRunIdentity.create(self.task_context)
            if identity is not None:
                from django_ray.workflow_progress import claim_workflow_run

                if claim_workflow_run(identity):
                    self.workflow_run_identity = identity
                    self.progress_actor = progress_actor_cls.remote(
                        identity.task_execution_pk,
                        identity.attempt_number,
                        identity.execution_generation,
                        identity.run_id,
                    )

    def submit_step(
        self,
        signature: Step,
        input_args: tuple[Any, ...],
        input_kwargs: dict[str, Any],
        node_id: str,
        dependencies: tuple[str, ...],
    ) -> Any:
        label = signature.callable_path.rsplit(".", 1)[-1]
        runtime_env_metadata: dict[str, Any] = {"mode": "inherit"}
        resolved_runtime_env = None
        if self.task_context is not None:
            runtime_env_metadata.update(
                {
                    "profile": self.task_context.runtime_env_profile,
                    "hash": self.task_context.runtime_env_hash,
                }
            )
        if signature.runtime_env is not None:
            from django_ray.runtime.runtime_env import (
                normalize_runtime_env,
                prepare_runtime_env_for_ray_core,
                resolve_runtime_env_profile,
            )

            if isinstance(signature.runtime_env, str):
                resolved_runtime_env = resolve_runtime_env_profile(signature.runtime_env)
            else:
                resolved_runtime_env = normalize_runtime_env(
                    signature.runtime_env,
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
            progress_actor.register.remote(
                node_id,
                label,
                signature.callable_path,
                list(dependencies),
                runtime_env_metadata,
                _json_safe(signature.ray_options),
            )
        options = {
            "name": f"django_ray.workflow:{label}",
            **signature.ray_options,
        }
        if resolved_runtime_env is not None:
            options["runtime_env"] = prepare_runtime_env_for_ray_core(resolved_runtime_env)
        object_ref = self.remote_step.options(**options).remote(
            signature.callable_path,
            signature.bootstrap_django,
            signature.bound_args,
            signature.bound_kwargs,
            input_kwargs,
            self.task_execution_pk,
            progress_actor,
            node_id,
            *input_args,
            workflow_run_identity=(
                self.workflow_run_identity.as_dict()
                if self.workflow_run_identity is not None
                else None
            ),
        )
        if progress_actor is not None:
            try:
                ray_task_id = object_ref.task_id().hex()
            except (AttributeError, RuntimeError):
                pass
            else:
                progress_actor.submitted.remote(node_id, label, ray_task_id)
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

    def _disable_progress_reporting(self, *, notify_actor: bool = True) -> None:
        """Stop local flushing and drain late reports in an obsolete actor."""
        progress_actor = self.progress_actor
        self.progress_actor = None
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
        try:
            self.progress_actor.register_map.remote(
                node_id,
                label,
                list(dependencies),
                max_concurrency,
                max_items,
            )
            self._map_progress_sent_at[node_id] = time.monotonic()
        except BaseException:
            # Workflow observability remains best effort.
            return

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
        try:
            self.progress_actor.map_progress.remote(
                node_id,
                label,
                submitted,
                completed,
                input_exhausted,
            )
            self._map_progress_sent_at[node_id] = now
        except BaseException:
            return

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
        try:
            self.progress_actor.map_progress.remote(
                node_id,
                label,
                submitted,
                completed,
                input_exhausted,
            )
            if failed:
                self.progress_actor.failed.remote(node_id, label, error or "Map failed")
            else:
                self.progress_actor.completed.remote(node_id, label)
            self._map_progress_sent_at.pop(node_id, None)
        except BaseException:
            return

    def _flush_progress(
        self,
        *,
        bypass_interval: bool = False,
        failed: bool = False,
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
        snapshot_ref = self.progress_actor.snapshot.remote()
        ready, _ = self.ray.wait([snapshot_ref], timeout=0.5)
        if not ready:
            return None

        try:
            snapshot = self.ray.get(ready[0])
        except Exception:
            # If the actor died (e.g. OOM), disable further tracking attempts
            # so we don't crash the workflow or repeatedly timeout.
            self._disable_progress_reporting(notify_actor=False)
            return None

        if failed:
            snapshot["state"] = "FAILED"
        revision = int(snapshot["revision"])
        if failed or revision != self.last_progress_revision:
            from django_ray.workflow_progress import persist_workflow_progress

            if not persist_workflow_progress(self.workflow_run_identity, snapshot):
                self._disable_progress_reporting()
                return None
            self.last_progress_revision = revision
        return snapshot

    def finish_progress(self, *, failed: bool = False) -> None:
        if self.progress_actor is None:
            return

        # Leaf event reporting is asynchronous. Give the in-memory actor a brief
        # chance to drain its mailbox before writing the terminal snapshot.
        for _attempt in range(10):
            snapshot = self._flush_progress(bypass_interval=True, failed=failed)
            if snapshot is None:
                return
            terminal = snapshot["completed_nodes"] + snapshot["failed_nodes"]
            if failed or terminal == snapshot["total_nodes"]:
                return
            time.sleep(0.05)


class WorkflowSignature(ABC):
    """A lazy, reusable workflow expression."""

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
        executor = _get_executor(use_ray)
        try:
            submission = self._submit(executor, args, kwargs, "0", ())
            result = executor.resolve(submission.value)
        except BaseException:
            executor.finish_progress(failed=True)
            raise
        executor.finish_progress()
        return result


@dataclass(frozen=True)
class Step(WorkflowSignature):
    """One importable callable submitted as a lightweight Ray task."""

    callable_path: str
    bound_args: tuple[Any, ...] = ()
    bound_kwargs: dict[str, Any] = field(default_factory=dict)
    bootstrap_django: bool = False
    ray_options: dict[str, Any] = field(default_factory=dict)
    runtime_env: str | dict[str, Any] | None = None

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
            ray_options={**self.ray_options, **ray_options},
            runtime_env=_clone_runtime_env(self.runtime_env),
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
        )


@dataclass(frozen=True)
class Chain(WorkflowSignature):
    """Run signatures sequentially, passing each result to the next."""

    signatures: tuple[WorkflowSignature, ...]

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
class Map(WorkflowSignature):
    """Fan out one signature over an iterable produced by an earlier stage."""

    signature: WorkflowSignature
    max_concurrency: int | None = None
    max_items: int | None = None
    cancel_timeout_seconds: float = 1.0

    def __post_init__(self) -> None:
        _validate_map_limit("max_concurrency", self.max_concurrency, minimum=1)
        _validate_map_limit("max_items", self.max_items, minimum=1)
        _validate_map_cancel_timeout(self.cancel_timeout_seconds)

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
        ordered_results: list[Any] = []
        submitted = 0
        completed = 0
        input_exhausted = False
        resolving_cleanup: tuple[Any, ...] = ()
        admitting_cleanup: list[Any] | None = None
        executor.map_started(
            node_id,
            label,
            dependencies,
            max_concurrency=self.max_concurrency,
            max_items=self.max_items,
        )

        try:
            while pending or not input_exhausted:
                while not input_exhausted and (
                    self.max_concurrency is None or len(pending) < self.max_concurrency
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

                ready_index = executor.wait_one([result.value for _, result, _ in pending])
                result_index, result, resolving_cleanup = pending.pop(ready_index)
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
        except BaseException as error:
            _close_iterator(iterator)
            cleanup_values = list(reversed(admitting_cleanup or ()))
            cleanup_values.extend(resolving_cleanup)
            cleanup_values.extend(cleanup for _, _, values in pending for cleanup in values)
            try:
                executor.cancel_and_drain(
                    cleanup_values,
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
                    error=str(error),
                )
            except BaseException:
                pass
            raise

        executor.map_finished(
            node_id,
            label,
            submitted=submitted,
            completed=completed,
            input_exhausted=True,
        )
        value = executor.store(ordered_results)
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


def _get_executor(use_ray: bool | None) -> _Executor:
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
        return _RayExecutor()
    return _LocalExecutor()


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
