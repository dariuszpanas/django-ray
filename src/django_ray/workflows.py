"""Composable Ray-native workflows with a single durable task boundary.

Workflow steps are submitted directly to Ray and do not create
``RayTaskExecution`` rows. Call a workflow from an ordinary Django task when
the complete workflow needs durable queueing, retries, and result storage.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

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


class _RayExecutor(_Executor):
    def __init__(self) -> None:
        import ray

        from django_ray.runtime.context import get_current_task_context
        from django_ray.runtime.remote import (
            WorkflowProgressActor,
            collect_workflow_results_remote,
            execute_workflow_step_remote,
        )

        self.ray = ray
        self.remote_step = ray.remote(execute_workflow_step_remote)
        self.remote_collect = ray.remote(collect_workflow_results_remote)
        self.task_context = get_current_task_context()
        self.task_execution_pk = (
            self.task_context.task_pk if self.task_context is not None else None
        )
        self.progress_actor = None
        self.last_progress_revision = -1
        if self.task_execution_pk is not None:
            self.progress_actor = ray.remote(num_cpus=0)(WorkflowProgressActor).remote(
                self.task_execution_pk
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
        if self.progress_actor is not None:
            self.progress_actor.register.remote(
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
            options["runtime_env"] = resolved_runtime_env.spec
        object_ref = self.remote_step.options(**options).remote(
            signature.callable_path,
            signature.bootstrap_django,
            signature.bound_args,
            signature.bound_kwargs,
            input_kwargs,
            self.task_execution_pk,
            self.progress_actor,
            node_id,
            *input_args,
        )
        if self.progress_actor is not None:
            try:
                ray_task_id = object_ref.task_id().hex()
            except (AttributeError, RuntimeError):
                pass
            else:
                self.progress_actor.submitted.remote(node_id, label, ray_task_id)
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

    def _flush_progress(
        self,
        *,
        force: bool = False,
        failed: bool = False,
    ) -> dict[str, Any] | None:
        if self.progress_actor is None or self.task_execution_pk is None:
            return None

        snapshot = self.ray.get(self.progress_actor.snapshot.remote())
        if failed:
            snapshot["state"] = "FAILED"
        revision = int(snapshot["revision"])
        if force or revision != self.last_progress_revision:
            from django_ray.models import RayTaskExecution

            RayTaskExecution.objects.filter(pk=self.task_execution_pk).update(
                progress_data=json.dumps(snapshot)
            )
            self.last_progress_revision = revision
        return snapshot

    def finish_progress(self, *, failed: bool = False) -> None:
        if self.progress_actor is None:
            return

        # Leaf event reporting is asynchronous. Give the in-memory actor a brief
        # chance to drain its mailbox before writing the terminal snapshot.
        for attempt in range(10):
            snapshot = self._flush_progress(force=attempt == 0, failed=failed)
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
        return _Submission(
            value=executor.submit_step(
                self,
                input_args,
                input_kwargs,
                node_id,
                dependencies,
            ),
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
            runtime_env=self.runtime_env,
        )

    def with_runtime_env(self, runtime_env: str | dict[str, Any] | None) -> Step:
        """Return a copy using a named profile or inline RuntimeEnv."""
        return Step(
            callable_path=self.callable_path,
            bound_args=self.bound_args,
            bound_kwargs=dict(self.bound_kwargs),
            bootstrap_django=self.bootstrap_django,
            ray_options=dict(self.ray_options),
            runtime_env=runtime_env,
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
        return _Submission(
            value=executor.collect([result.value for result in results]),
            terminal_node_ids=tuple(
                node for result in results for node in result.terminal_node_ids
            ),
        )


@dataclass(frozen=True)
class Map(WorkflowSignature):
    """Fan out one signature over an iterable produced by an earlier stage."""

    signature: WorkflowSignature

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
        return _Submission(
            value=executor.collect([result.value for result in results]),
            terminal_node_ids=(
                tuple(node for result in results for node in result.terminal_node_ids)
                if results
                else dependencies
            ),
        )


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
        runtime_env=runtime_env,
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
    """Create a dynamic fan-out over the preceding stage's iterable."""
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
