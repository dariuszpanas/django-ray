"""Shared fake executors, progress actors, and helpers for workflow unit tests."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from django_ray.runtime.context import (
    WorkflowRunIdentity,
)
from django_ray.runtime.remote import WorkflowProgressActor
from django_ray.workflow.progress.protocol import (
    WorkflowProgressEventKind,
    decode_workflow_progress_event,
    prepare_workflow_progress_event,
)
from django_ray.workflows import (
    _Executor,
)


class DelayedFirstSnapshotProgressActor(WorkflowProgressActor):
    """Force the coordinator through its terminal snapshot retry path."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._first_snapshot = True

    def snapshot(self) -> dict[str, Any]:
        if self._first_snapshot:
            self._first_snapshot = False
            time.sleep(0.75)
        return super().snapshot()


class _ConcurrencyTracker:
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    def enter(self) -> None:
        self.active += 1
        self.peak = max(self.peak, self.active)

    def exit(self) -> None:
        self.active -= 1

    def reached(self, minimum: int) -> bool:
        return self.peak >= minimum

    def snapshot(self) -> tuple[int, int]:
        return self.active, self.peak


class _LifecycleTracker:
    def __init__(self) -> None:
        self.events: list[str] = []

    def record(self, event: str) -> None:
        self.events.append(event)

    def contains(self, event: str) -> bool:
        return event in self.events

    def snapshot(self) -> list[str]:
        return list(self.events)


@dataclass
class _GraphExecutor(_Executor):
    nodes: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def submit_step(
        self,
        signature,
        input_args,
        input_kwargs,
        node_id,
        dependencies,
    ):
        self.nodes[node_id] = dependencies
        callable_obj = __import__(
            signature.callable_path.rsplit(".", 1)[0],
            fromlist=[signature.callable_path.rsplit(".", 1)[1]],
        )
        function = getattr(callable_obj, signature.callable_path.rsplit(".", 1)[1])
        kwargs = {**input_kwargs, **signature.bound_kwargs}
        return function(
            *input_args,
            *signature.bound_args,
            **kwargs,
        )

    def collect(self, values):
        return values

    def resolve(self, value):
        return value


class _Ref:
    def __init__(self, value: Any) -> None:
        self.value = value


class _RecordingIngest:
    def __init__(self) -> None:
        self.calls: list[bytes] = []

    def remote(self, wire: bytes) -> None:
        assert type(wire) is bytes
        self.calls.append(wire)


class _IngestOnlyProgressActor:
    def __init__(self) -> None:
        self.ingest = _RecordingIngest()


def _workflow_identity(
    *,
    task_execution_pk: int = 42,
    attempt_number: int = 2,
    execution_generation: int = 5,
    run_id: str = "00000000-0000-0000-0000-000000000217",
) -> WorkflowRunIdentity:
    return WorkflowRunIdentity(
        task_execution_pk=task_execution_pk,
        attempt_number=attempt_number,
        execution_generation=execution_generation,
        run_id=run_id,
    )


def _workflow_plan_summary() -> dict[str, Any]:
    return {
        "plan_format": "django-ray.workflow-plan",
        "plan_format_version": 1,
        "fingerprint": "sha256:" + "a" * 64,
        "definition_name": "workflow:test-progress",
        "definition_revision": "sha256:" + ("b" * 64),
        "topology_class": "dynamic",
        "node_count": 2,
    }


def _ingest_event(
    actor: WorkflowProgressActor,
    identity: WorkflowRunIdentity,
    kind: WorkflowProgressEventKind,
    payload: Mapping[str, Any],
) -> None:
    actor.ingest(
        prepare_workflow_progress_event(
            identity.as_dict(),
            kind,
            payload,
        )
    )


def _decoded_ingests(
    actor: _IngestOnlyProgressActor,
    identity: WorkflowRunIdentity,
) -> list[Any]:
    return [
        decode_workflow_progress_event(
            wire,
            expected_run_identity=identity.as_dict(),
        )
        for wire in actor.ingest.calls
    ]


class _RemoteFunction:
    def __init__(self, ray: _FakeRay, function: Any) -> None:
        self.ray = ray
        self.function = function
        self.options_seen: dict[str, Any] = {}

    def options(self, **options: Any) -> _RemoteFunction:
        self.options_seen = options
        self.ray.options_seen.append(options)
        return self

    def remote(self, *args: Any, **kwargs: Any) -> _Ref:
        resolved_args = tuple(arg.value if isinstance(arg, _Ref) else arg for arg in args)
        resolved_kwargs = {
            key: value.value if isinstance(value, _Ref) else value for key, value in kwargs.items()
        }
        self.ray.submissions += 1
        return _Ref(self.function(*resolved_args, **resolved_kwargs))


class _FakeRay:
    def __init__(self, *, initialized: bool = True) -> None:
        self.initialized = initialized
        self.submissions = 0
        self.get_calls = 0
        self.options_seen: list[dict[str, Any]] = []
        self.init_calls: list[dict[str, Any]] = []
        self.remote_calls: list[dict[str, Any]] = []
        self.remote_invocations: list[tuple[str, int]] = []
        self.wait_sizes: list[int] = []
        self.cancelled: list[_Ref] = []
        self.put_calls = 0

    def is_initialized(self) -> bool:
        return self.initialized

    def init(self, **kwargs: Any) -> None:
        self.init_calls.append(kwargs)
        self.initialized = True

    def remote(self, *args, **kwargs: Any):
        self.remote_calls.append(kwargs)

        def _decorator(fn):
            fake = self

            class _RemoteCallable:
                @staticmethod
                def remote(*args: Any, **kw: Any) -> _Ref:
                    fake.remote_invocations.append((fn.__name__, len(args) + len(kw)))
                    resolved_args = tuple(
                        arg.value if isinstance(arg, _Ref) else arg for arg in args
                    )
                    resolved_kwargs = {
                        key: value.value if isinstance(value, _Ref) else value
                        for key, value in kw.items()
                    }
                    fake.submissions += 1
                    return _Ref(fn(*resolved_args, **resolved_kwargs))

                def options(self, **kw: Any):
                    fake.options_seen.append(kw)
                    return self

            return _RemoteCallable()

        if args and callable(args[0]):
            return _decorator(args[0])
        return _decorator

    def get(self, ref: _Ref) -> Any:
        self.get_calls += 1
        return ref.value

    def put(self, value: Any) -> _Ref:
        self.put_calls += 1
        return _Ref(value)

    def wait(
        self,
        refs: list[_Ref],
        *,
        num_returns: int = 1,
        timeout: float | None = None,
        fetch_local: bool = True,
    ) -> tuple[list[_Ref], list[_Ref]]:
        del fetch_local
        if timeout == 0:
            # Non-blocking cleanup readiness probes treat still-pending fake
            # refs as unready unless a test overrides readiness explicitly.
            return [], list(refs)
        self.wait_sizes.append(len(refs))
        return refs[:num_returns], refs[num_returns:]

    def cancel(
        self,
        ref: _Ref,
        *,
        force: bool,
        recursive: bool,
    ) -> None:
        assert force is False
        assert recursive is True
        self.cancelled.append(ref)


class _PendingValue:
    def __init__(self, value: Any = None, error: BaseException | None = None) -> None:
        self.value = value
        self.error = error


@dataclass
class _InstrumentedWindowExecutor(_Executor):
    """Deterministic future executor used to prove the map admission invariant."""

    live: set[_PendingValue] = field(default_factory=set)
    peak_live: int = 0
    submissions: int = 0
    cancelled: list[_PendingValue] = field(default_factory=list)
    wait_sizes: list[int] = field(default_factory=list)
    finished_errors: list[str | None] = field(default_factory=list)

    def submit_step(
        self,
        signature,
        input_args,
        input_kwargs,
        node_id,
        dependencies,
    ):
        del node_id, dependencies
        callable_obj = __import__(
            signature.callable_path.rsplit(".", 1)[0],
            fromlist=[signature.callable_path.rsplit(".", 1)[1]],
        )
        function = getattr(callable_obj, signature.callable_path.rsplit(".", 1)[1])
        kwargs = {**input_kwargs, **signature.bound_kwargs}
        try:
            value = function(*input_args, *signature.bound_args, **kwargs)
            ref = _PendingValue(value=value)
        except BaseException as error:
            ref = _PendingValue(error=error)
        self.live.add(ref)
        self.submissions += 1
        self.peak_live = max(self.peak_live, len(self.live))
        return ref

    def collect(self, values):
        raise AssertionError("bounded map must not use an all-results collector")

    def resolve(self, value):
        if not isinstance(value, _PendingValue):
            return value
        self.live.discard(value)
        if value.error is not None:
            raise value.error
        return value.value

    def wait_one(self, values):
        self.wait_sizes.append(len(values))
        # Completing the newest item first exercises ordered reassembly under skew.
        return len(values) - 1

    def cancel_and_drain(self, values, *, timeout_seconds):
        assert timeout_seconds >= 0
        self.cancelled.extend(values)
        self.live.difference_update(values)

    def map_finished(
        self,
        node_id,
        label,
        *,
        submitted,
        completed,
        input_exhausted,
        failed=False,
        error=None,
    ):
        del node_id, label, submitted, completed, input_exhausted, failed
        self.finished_errors.append(error)


class _CleanupRef:
    def __init__(self, label: str, error: BaseException | None = None) -> None:
        self.label = label
        self.error = error


@dataclass
class _CleanupTrackingExecutor(_Executor):
    """Model dependency refs separately from a logical item's terminal ref."""

    cleaned: list[_CleanupRef] = field(default_factory=list)

    def submit_step(
        self,
        signature,
        input_args,
        input_kwargs,
        node_id,
        dependencies,
    ):
        del input_args, input_kwargs, dependencies
        if signature.callable_path.endswith(".cleanup_submission_failure"):
            raise RuntimeError("original submission failure")
        error = (
            RuntimeError("original cleanup failure")
            if signature.callable_path.endswith(".cleanup_failure")
            else None
        )
        return _CleanupRef(node_id, error)

    def collect(self, values):
        error = next((value.error for value in values if value.error is not None), None)
        return _CleanupRef("collector", error)

    def resolve(self, value):
        if not isinstance(value, _CleanupRef):
            return value
        if value.error is not None:
            raise value.error
        return value

    def wait_one(self, values):
        return 0

    def cancel_and_drain(self, values, *, timeout_seconds):
        assert timeout_seconds == 0.25
        self.cleaned.extend(values)


def _strict_workflow_fixture_identity(execution, *, profile=None):
    from django_ray.runtime.runtime_env import normalize_runtime_env
    from django_ray.workflow.plans import runtime_env_plan_identity

    return {
        "task_id": execution.task_id,
        "execution_protocol_version": 1,
        "strict_execution_request": True,
        "runtime_env_plan_identity": runtime_env_plan_identity(
            normalize_runtime_env({}, profile=profile)
        ).as_transport_dict(),
    }
