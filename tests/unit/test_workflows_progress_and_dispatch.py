"""Unit tests for workflow progress polling, causal fences, and Ray executor dispatch."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from django_ray.runtime.context import (
    WORKFLOW_PROGRESS_SCHEMA_VERSION,
    DurableTaskContext,
    WorkflowRunIdentity,
    durable_task_execution,
)
from django_ray.workflow.progress.limits import WORKFLOW_PROGRESS_LIMITS_V1
from django_ray.workflow.progress.protocol import (
    WorkflowProgressEventKind,
    decode_workflow_progress_event,
)
from django_ray.workflows import (
    WorkflowDefinitionError,
    _failed_snapshot_has_causally_complete_ancestors,
    _get_executor,
    _RayExecutor,
    _workflow_progress_policy,
    step,
)
from tests.unit._workflows_helpers import (
    _decoded_ingests,
    _FakeRay,
    _GraphExecutor,
    _IngestOnlyProgressActor,
    _Ref,
    _workflow_identity,
)
from tests.unit.test_workflows import (
    echo_workflow_progress_policy,
    increment,
    preview_increment,
)


@pytest.fixture
def workflow_progress_warning_records(monkeypatch) -> list[dict[str, Any]]:
    """Capture structured warning inputs without relying on logger propagation."""
    records: list[dict[str, Any]] = []

    class _RecordingLogger:
        def __init__(self, context: dict[str, Any]) -> None:
            self.context = context

        def warning(self, message: str, *, extra: dict[str, Any]) -> None:
            records.append(
                {
                    "message": message,
                    **self.context,
                    **extra,
                }
            )

    def get_logger(name: str, **context: Any) -> _RecordingLogger:
        assert name == "django_ray.workflows"
        return _RecordingLogger(context)

    monkeypatch.setattr("django_ray.logging.get_logger", get_logger)
    return records


def test_invalid_workflow_progress_policy_fails_before_plan_materialization(
    monkeypatch,
) -> None:
    materialized = False

    def record_materialization(*args, **kwargs):
        del args, kwargs
        nonlocal materialized
        materialized = True
        raise AssertionError("policy validation must run first")

    monkeypatch.setattr(
        "django_ray.workflow.plans.materialize_workflow_plan",
        record_materialization,
    )

    with pytest.raises(WorkflowDefinitionError, match="must be one of"):
        step(increment).with_progress_reporting("sampled")

    assert materialized is False


def test_progress_policy_named_application_kwarg_is_not_reserved() -> None:
    assert (
        step(echo_workflow_progress_policy).run(
            use_ray=False,
            workflow_progress_policy="business-value",
        )
        == "business-value"
    )


def test_workflow_progress_policy_uses_configured_default(settings) -> None:
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "WORKFLOW_PROGRESS_REPORTING_POLICY": "disabled",
    }

    assert _workflow_progress_policy(None) == "disabled"
    assert _workflow_progress_policy("full") == "full"
    assert _workflow_progress_policy("terminal_only") == "terminal_only"

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "WORKFLOW_PROGRESS_REPORTING_POLICY": "terminal_only",
    }
    assert _workflow_progress_policy(None) == "terminal_only"


def test_terminal_only_per_workflow_override_reaches_the_ray_executor(
    monkeypatch,
) -> None:
    executor = _GraphExecutor()
    bindings: list[tuple[str, str]] = []

    def bind_plan(materialized_plan, *, requested_policy, reporting_policy):
        del materialized_plan
        bindings.append((requested_policy, reporting_policy))

    monkeypatch.setattr(executor, "bind_plan", bind_plan)
    monkeypatch.setattr("django_ray.workflows._get_executor", lambda use_ray: executor)

    result = step(increment).with_progress_reporting("terminal_only").run(1, use_ray=True)

    assert result == 2
    assert bindings == [("dynamic_tasks", "terminal_only")]


def test_ray_job_workflow_lazily_initializes_ray(monkeypatch) -> None:
    fake_ray = _FakeRay(initialized=False)
    executor = object()
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    monkeypatch.setattr("django_ray.workflows._RayExecutor", lambda: executor)

    with durable_task_execution(42, ray_job_driver=True):
        assert _get_executor(True) is executor

    assert fake_ray.init_calls == [{"address": "auto", "ignore_reinit_error": True}]


def test_ray_executor_progress_flush_handles_unavailable_actor() -> None:
    executor = object.__new__(_RayExecutor)
    executor.progress_actor = None
    executor.task_execution_pk = 1
    executor.workflow_run_identity = None

    assert executor._flush_progress() is None

    snapshot_ref = object()
    executor.progress_actor = SimpleNamespace(snapshot=SimpleNamespace(remote=lambda: snapshot_ref))
    executor.workflow_run_identity = object()
    executor.ray = SimpleNamespace(wait=lambda refs, timeout: ([], refs))
    assert executor._flush_progress(bypass_interval=True) is None

    executor.ray = SimpleNamespace(
        wait=lambda refs, timeout: (refs, []),
        get=lambda ref: (_ for _ in ()).throw(RuntimeError("actor died")),
    )
    assert executor._flush_progress(bypass_interval=True) is None
    assert executor.progress_actor is None


@pytest.mark.parametrize("failure_point", ["submit", "wait"])
def test_ray_executor_progress_flush_contains_snapshot_rpc_failures(
    failure_point,
    workflow_progress_warning_records,
) -> None:
    snapshot_ref = object()

    def submit():
        if failure_point == "submit":
            raise RuntimeError("submit failed")
        return snapshot_ref

    def wait(refs, timeout):
        del refs, timeout
        if failure_point == "wait":
            raise RuntimeError("wait failed")
        raise AssertionError("wait should fail in this test")

    executor = object.__new__(_RayExecutor)
    executor.progress_actor = SimpleNamespace(
        snapshot=SimpleNamespace(remote=submit),
    )
    executor.workflow_run_identity = WorkflowRunIdentity(
        task_execution_pk=9,
        attempt_number=1,
        execution_generation=1,
        run_id=f"snapshot-{failure_point}-failure",
    )
    executor.last_progress_flush_at = 0.0
    executor.ray = SimpleNamespace(wait=wait)

    assert executor._flush_progress(bypass_interval=True) is None

    assert executor.progress_actor is None
    assert executor._pending_progress_snapshot_ref is None
    assert workflow_progress_warning_records == [
        {
            "message": "Workflow progress reporting became unavailable",
            "component": "workflow_progress",
            "task_execution_pk": 9,
            "workflow_run_id": f"snapshot-{failure_point}-failure",
            "reason": "snapshot_rpc_failed",
        }
    ]


def test_ray_executor_throttles_high_cardinality_progress_snapshots(
    monkeypatch,
    settings,
) -> None:
    identity = WorkflowRunIdentity(
        task_execution_pk=1,
        attempt_number=1,
        execution_generation=1,
        run_id="progress-throttle-run",
    )
    persisted: list[dict[str, Any]] = []

    def persist(reported_identity, snapshot):
        assert reported_identity == identity
        persisted.append(snapshot)
        return True

    monkeypatch.setattr(
        "django_ray.workflow.progress.runs.persist_workflow_progress",
        persist,
    )
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "WORKFLOW_PROGRESS_FLUSH_SECONDS": 1,
    }
    now = [100.0]
    monkeypatch.setattr("django_ray.workflows.time.monotonic", lambda: now[0])
    snapshot_ref = object()
    work_ref = object()
    snapshot_calls = 0

    class _SnapshotMethod:
        def remote(self):
            nonlocal snapshot_calls
            snapshot_calls += 1
            return snapshot_ref

    class _ProgressRay:
        def __init__(self) -> None:
            self.work_wait_calls = 0

        def wait(self, refs, **kwargs):
            del kwargs
            if refs == [work_ref]:
                self.work_wait_calls += 1
            return refs, []

        def get(self, ref):
            if ref is snapshot_ref:
                return {
                    "schema_version": 2,
                    "run_identity": identity.as_dict(),
                    "revision": 1,
                    "state": "RUNNING",
                    "completed_nodes": 0,
                    "failed_nodes": 0,
                    "total_nodes": 1,
                }
            assert ref is work_ref
            return "ready"

    executor = object.__new__(_RayExecutor)
    executor.progress_actor = SimpleNamespace(snapshot=_SnapshotMethod())
    executor.workflow_run_identity = identity
    executor.last_progress_revision = 0
    executor.last_progress_flush_at = now[0]
    executor.ray = _ProgressRay()

    for index in range(5_000):
        if index == 2_500:
            now[0] += 1.0
        assert executor.wait_one([work_ref]) == 0
        assert executor.resolve_ready(work_ref) == "ready"

    assert snapshot_calls == 1
    assert executor.ray.work_wait_calls == 5_000

    executor._flush_progress(bypass_interval=True)

    assert snapshot_calls == 2
    assert persisted == []


def test_flush_progress_reuses_pending_actor_snapshot_request(monkeypatch) -> None:
    identity = object()
    persisted: list[dict[str, Any]] = []
    snapshot_ref = object()
    snapshot_calls = 0
    wait_calls = 0

    def persist(reported_identity, snapshot):
        assert reported_identity is identity
        persisted.append(snapshot)
        return True

    monkeypatch.setattr(
        "django_ray.workflow.progress.runs.persist_workflow_progress",
        persist,
    )

    class _SnapshotMethod:
        def remote(self):
            nonlocal snapshot_calls
            snapshot_calls += 1
            return snapshot_ref

    snapshot = {
        "revision": 3,
        "completed_nodes": 1,
        "failed_nodes": 0,
        "total_nodes": 1,
    }

    def wait(refs, timeout):
        nonlocal wait_calls
        assert refs == [snapshot_ref]
        assert timeout == 0.5
        wait_calls += 1
        return ([], refs) if wait_calls == 1 else (refs, [])

    executor = object.__new__(_RayExecutor)
    executor.progress_actor = SimpleNamespace(snapshot=_SnapshotMethod())
    executor.workflow_run_identity = identity
    executor.last_progress_revision = -1
    executor.last_progress_flush_at = 0.0
    executor.ray = SimpleNamespace(wait=wait, get=lambda ref: dict(snapshot))

    assert executor._flush_progress(bypass_interval=True) is None
    assert executor._flush_progress(bypass_interval=True) == snapshot

    assert snapshot_calls == 1
    assert wait_calls == 2
    assert persisted == []
    assert executor._pending_progress_snapshot_ref is None


def test_flush_progress_does_not_rewrite_one_unchanged_failed_snapshot(monkeypatch) -> None:
    identity = object()
    snapshot_ref = object()
    persisted: list[dict[str, Any]] = []
    snapshot = {
        "revision": 3,
        "completed_nodes": 0,
        "failed_nodes": 1,
        "total_nodes": 2,
    }

    monkeypatch.setattr(
        "django_ray.workflow.progress.runs.persist_workflow_progress",
        lambda reported_identity, value: persisted.append(value) or reported_identity is identity,
    )
    executor = object.__new__(_RayExecutor)
    executor.progress_actor = SimpleNamespace(snapshot=SimpleNamespace(remote=lambda: snapshot_ref))
    executor.workflow_run_identity = identity
    executor.last_progress_revision = -1
    executor.last_progress_flush_at = 0.0
    executor.ray = SimpleNamespace(
        wait=lambda refs, timeout: (refs, []),
        get=lambda ref: dict(snapshot),
    )

    assert executor._flush_progress(bypass_interval=True, failed=True) is not None
    assert executor._flush_progress(bypass_interval=True, failed=True) is not None

    assert persisted == []


def test_finish_progress_polls_without_rewriting_unchanged_snapshot(monkeypatch) -> None:
    identity = object()
    persisted: list[dict[str, Any]] = []
    snapshot_ref = object()
    snapshot_calls = 0

    def persist(reported_identity, snapshot):
        assert reported_identity is identity
        persisted.append(snapshot)
        return True

    monkeypatch.setattr(
        "django_ray.workflow.progress.runs.persist_workflow_progress",
        persist,
    )

    class _SnapshotMethod:
        def remote(self):
            nonlocal snapshot_calls
            snapshot_calls += 1
            return snapshot_ref

    executor = object.__new__(_RayExecutor)
    executor.progress_actor = SimpleNamespace(snapshot=_SnapshotMethod())
    executor.workflow_run_identity = identity
    executor.last_progress_revision = -1
    executor.last_progress_flush_at = 0.0
    executor.ray = SimpleNamespace(
        wait=lambda refs, timeout: (refs, []),
        get=lambda ref: {
            "revision": 4 if snapshot_calls >= 3 else 3,
            "completed_nodes": 1 if snapshot_calls >= 3 else 0,
            "failed_nodes": 0,
            "total_nodes": 1,
        },
    )
    sleeps: list[float] = []
    monkeypatch.setattr("django_ray.workflows.time.sleep", sleeps.append)

    executor.finish_progress()

    assert snapshot_calls == 3
    assert persisted == []
    assert sleeps == [0.05, 0.05]


def test_finish_progress_retries_transient_snapshot_unavailability(monkeypatch) -> None:
    executor = object.__new__(_RayExecutor)
    executor.progress_actor = object()
    executor.workflow_run_identity = None
    snapshots = iter(
        [
            None,
            None,
            {"completed_nodes": 1, "failed_nodes": 0, "total_nodes": 1},
        ]
    )
    sleeps: list[float] = []
    monkeypatch.setattr(executor, "_flush_progress", lambda **kwargs: next(snapshots))
    monkeypatch.setattr("django_ray.workflows.time.sleep", sleeps.append)

    executor.finish_progress()

    assert sleeps == [0.05, 0.05]


def test_finish_progress_reports_permanent_snapshot_unavailability(
    monkeypatch,
    settings,
    workflow_progress_warning_records,
) -> None:
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "WORKFLOW_PROGRESS_TERMINAL_FLUSH_TIMEOUT_SECONDS": 1,
    }
    executor = object.__new__(_RayExecutor)
    executor.progress_actor = object()
    executor.workflow_run_identity = WorkflowRunIdentity(
        task_execution_pk=7,
        attempt_number=2,
        execution_generation=3,
        run_id="terminal-timeout-run",
    )
    clock = [0.0]
    flush_calls = 0
    sleeps: list[float] = []
    wait_timeouts: list[float] = []

    def flush(**kwargs):
        nonlocal flush_calls
        assert kwargs["bypass_interval"] is True
        assert kwargs["failed"] is False
        flush_calls += 1
        wait_timeout = kwargs["wait_timeout_seconds"]
        wait_timeouts.append(wait_timeout)
        clock[0] += wait_timeout
        return None

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(executor, "_flush_progress", flush)
    monkeypatch.setattr("django_ray.workflows.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("django_ray.workflows.time.sleep", sleep)

    monkeypatch.setattr(
        "django_ray.workflow.progress.publication.publish_unavailable_terminal_workflow_progress",
        lambda *args, **kwargs: True,
    )
    executor.finish_progress()

    assert flush_calls == 2
    assert sleeps == [0.05]
    assert wait_timeouts == pytest.approx([0.5, 0.45])
    assert clock[0] == pytest.approx(1.0)
    assert workflow_progress_warning_records == [
        {
            "message": ("Workflow terminal progress did not complete before the flush deadline"),
            "component": "workflow_progress",
            "task_execution_pk": 7,
            "workflow_run_id": "terminal-timeout-run",
            "reason": "snapshot_unavailable",
            "timeout_seconds": 1.0,
            "failed_workflow": False,
        }
    ]


def test_finish_progress_forwards_remaining_deadline_to_ray_wait(
    monkeypatch,
    settings,
) -> None:
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "WORKFLOW_PROGRESS_TERMINAL_FLUSH_TIMEOUT_SECONDS": 1,
    }
    snapshot_ref = object()
    wait_timeouts: list[float] = []
    clock = [0.0]

    def wait(refs, timeout):
        assert refs == [snapshot_ref]
        wait_timeouts.append(timeout)
        clock[0] += timeout
        return [], refs

    def sleep(seconds):
        clock[0] += seconds

    executor = object.__new__(_RayExecutor)
    executor.progress_actor = SimpleNamespace(
        snapshot=SimpleNamespace(remote=lambda: snapshot_ref),
    )
    executor.workflow_run_identity = WorkflowRunIdentity(
        task_execution_pk=7,
        attempt_number=2,
        execution_generation=3,
        run_id="terminal-budget-run",
    )
    executor.last_progress_flush_at = 0.0
    executor.ray = SimpleNamespace(wait=wait)
    monkeypatch.setattr("django_ray.workflows.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("django_ray.workflows.time.sleep", sleep)

    executor.finish_progress()

    assert wait_timeouts == pytest.approx([0.5, 0.45])
    assert clock[0] == pytest.approx(1.0)
    assert executor._pending_progress_snapshot_ref is None


def test_finish_progress_stops_immediately_after_permanent_actor_failure(
    monkeypatch,
    workflow_progress_warning_records,
) -> None:
    snapshot_ref = object()
    snapshot_calls = 0
    sleeps: list[float] = []
    fallback_calls = []

    class _SnapshotMethod:
        def remote(self):
            nonlocal snapshot_calls
            snapshot_calls += 1
            return snapshot_ref

    executor = object.__new__(_RayExecutor)
    executor.progress_actor = SimpleNamespace(snapshot=_SnapshotMethod())
    executor.workflow_run_identity = WorkflowRunIdentity(
        task_execution_pk=8,
        attempt_number=1,
        execution_generation=1,
        run_id="terminal-actor-failure",
    )
    executor.last_progress_flush_at = 0.0
    executor.ray = SimpleNamespace(
        wait=lambda refs, timeout: (refs, []),
        get=lambda ref: (_ for _ in ()).throw(RuntimeError("actor died")),
    )
    monkeypatch.setattr("django_ray.workflows.time.sleep", sleeps.append)
    monkeypatch.setattr(
        executor,
        "_publish_unavailable_terminal_progress",
        lambda **kwargs: fallback_calls.append(kwargs),
    )

    executor.finish_progress()

    assert snapshot_calls == 1
    assert sleeps == []
    assert fallback_calls == [{"failed": False, "limit_exceeded": False}]
    assert executor.progress_actor is None
    assert workflow_progress_warning_records == [
        {
            "message": "Workflow progress reporting became unavailable",
            "component": "workflow_progress",
            "task_execution_pk": 8,
            "workflow_run_id": "terminal-actor-failure",
            "reason": "snapshot_get_failed",
        }
    ]


def test_finish_progress_contains_persistence_failure_without_leaking_exception(
    monkeypatch,
    workflow_progress_warning_records,
) -> None:
    snapshot_ref = object()
    disabled: list[bool] = []
    sleeps: list[float] = []

    def persist(reported_identity, snapshot, **kwargs):
        del reported_identity, snapshot
        raise RuntimeError("password=do-not-leak")

    monkeypatch.setattr(
        "django_ray.workflow.progress.publication.publish_terminal_workflow_progress",
        persist,
    )
    executor = object.__new__(_RayExecutor)
    executor.progress_actor = SimpleNamespace(
        snapshot=SimpleNamespace(remote=lambda: snapshot_ref),
        disable=SimpleNamespace(remote=lambda: disabled.append(True)),
    )
    executor.workflow_run_identity = WorkflowRunIdentity(
        task_execution_pk=8,
        attempt_number=1,
        execution_generation=1,
        run_id="terminal-persistence-failure",
    )
    executor.last_progress_revision = -1
    executor.last_progress_flush_at = 0.0
    executor.ray = SimpleNamespace(
        wait=lambda refs, timeout: (refs, []),
        get=lambda ref: {
            "revision": 1,
            "completed_nodes": 1,
            "failed_nodes": 0,
            "total_nodes": 1,
        },
    )
    monkeypatch.setattr("django_ray.workflows.time.sleep", sleeps.append)

    monkeypatch.setattr(
        "django_ray.workflow.progress.publication.publish_unavailable_terminal_workflow_progress",
        lambda *args, **kwargs: True,
    )
    executor.finish_progress()

    assert executor.progress_actor is not None
    assert disabled == []
    assert sleeps == []
    assert workflow_progress_warning_records == [
        {
            "message": "Workflow terminal publication was not completed",
            "component": "workflow_progress",
            "task_execution_pk": 8,
            "workflow_run_id": "terminal-persistence-failure",
            "reason": "publication_failed",
        }
    ]
    assert "do-not-leak" not in json.dumps(workflow_progress_warning_records)


def test_finish_progress_reports_incomplete_terminal_snapshot(
    monkeypatch,
    settings,
    workflow_progress_warning_records,
) -> None:
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "WORKFLOW_PROGRESS_TERMINAL_FLUSH_TIMEOUT_SECONDS": 1,
    }
    executor = object.__new__(_RayExecutor)
    executor.progress_actor = object()
    executor.workflow_run_identity = None
    clock = [0.0]

    def flush(**kwargs):
        clock[0] += kwargs["wait_timeout_seconds"]
        return {"completed_nodes": 0, "failed_nodes": 0, "total_nodes": 1}

    def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(executor, "_flush_progress", flush)
    monkeypatch.setattr("django_ray.workflows.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("django_ray.workflows.time.sleep", sleep)

    executor.finish_progress()

    assert clock[0] == pytest.approx(1.0)
    assert workflow_progress_warning_records == [
        {
            "message": ("Workflow terminal progress did not complete before the flush deadline"),
            "component": "workflow_progress",
            "task_execution_pk": None,
            "workflow_run_id": None,
            "reason": "snapshot_incomplete",
            "timeout_seconds": 1.0,
            "failed_workflow": False,
        }
    ]


def test_finish_progress_waits_for_terminal_snapshot(monkeypatch) -> None:
    executor = object.__new__(_RayExecutor)
    executor.progress_actor = object()
    executor.workflow_run_identity = None
    snapshots = iter(
        [
            {"completed_nodes": 0, "failed_nodes": 0, "total_nodes": 1},
            {"completed_nodes": 1, "failed_nodes": 0, "total_nodes": 1},
        ]
    )
    sleeps: list[float] = []
    monkeypatch.setattr(executor, "_flush_progress", lambda **kwargs: next(snapshots))
    monkeypatch.setattr("django_ray.workflows.time.sleep", sleeps.append)

    executor.finish_progress()

    assert sleeps == [0.05]


def test_finish_progress_waits_for_failed_node_evidence_when_pilot_enabled(
    monkeypatch,
    settings,
) -> None:
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
    }
    executor = object.__new__(_RayExecutor)
    executor.progress_actor = object()
    executor.workflow_run_identity = None
    snapshots = iter(
        [
            {
                "completed_nodes": 0,
                "failed_nodes": 0,
                "total_nodes": 2,
                "graph": {
                    "nodes": [
                        {"node_id": "0.0", "state": "PENDING"},
                        {"node_id": "0.1", "state": "PENDING"},
                    ],
                    "edges": [{"source": "0.0", "target": "0.1"}],
                },
            },
            {
                "completed_nodes": 0,
                "failed_nodes": 1,
                "total_nodes": 2,
                "graph": {
                    "nodes": [
                        {"node_id": "0.0", "state": "PENDING"},
                        {"node_id": "0.1", "state": "FAILED"},
                    ],
                    "edges": [{"source": "0.0", "target": "0.1"}],
                },
            },
            {
                "completed_nodes": 1,
                "failed_nodes": 1,
                "total_nodes": 2,
                "graph": {
                    "nodes": [
                        {"node_id": "0.0", "state": "SUCCEEDED"},
                        {"node_id": "0.1", "state": "FAILED"},
                    ],
                    "edges": [{"source": "0.0", "target": "0.1"}],
                },
            },
        ]
    )
    sleeps: list[float] = []
    monkeypatch.setattr(executor, "_flush_progress", lambda **kwargs: next(snapshots))
    monkeypatch.setattr("django_ray.workflows.time.sleep", sleeps.append)

    executor.finish_progress(failed=True)

    assert sleeps == [0.05, 0.05]


def test_failed_snapshot_causal_fence_ignores_unrelated_pending_branch() -> None:
    assert _failed_snapshot_has_causally_complete_ancestors(
        {
            "graph": {
                "nodes": [
                    {"node_id": "0.0", "state": "SUCCEEDED"},
                    {"node_id": "0.1", "state": "FAILED"},
                    {"node_id": "0.2", "state": "PENDING"},
                ],
                "edges": [
                    {"source": "0.0", "target": "0.1"},
                    {"source": "0.0", "target": "0.2"},
                ],
            }
        }
    )


@pytest.mark.parametrize(
    "snapshot",
    [
        {},
        {"graph": {"nodes": [], "edges": []}},
        {
            "graph": {
                "nodes": [{"node_id": "0.0", "state": {"invalid": True}}],
                "edges": [],
            }
        },
        {
            "graph": {
                "nodes": [
                    {"node_id": "0.0", "state": "PENDING"},
                    {"node_id": "0.1", "state": "FAILED"},
                ],
                "edges": [{"source": "0.0", "target": "0.1"}],
            }
        },
        {
            "graph": {
                "nodes": [{"node_id": "0.0", "state": "FAILED"}],
                "edges": [{"source": "0.0", "target": "missing"}],
            }
        },
    ],
)
def test_failed_snapshot_causal_fence_rejects_incomplete_evidence(
    snapshot: dict[str, Any],
) -> None:
    assert not _failed_snapshot_has_causally_complete_ancestors(snapshot)


def test_failed_snapshot_causal_fence_bounds_cycle_walk() -> None:
    node_count = 128
    assert not _failed_snapshot_has_causally_complete_ancestors(
        {
            "graph": {
                "nodes": [
                    {
                        "node_id": f"0.{index}",
                        "state": "FAILED" if index == node_count - 1 else "SUCCEEDED",
                    }
                    for index in range(node_count)
                ],
                "edges": [
                    {
                        "source": f"0.{index}",
                        "target": f"0.{(index + 1) % node_count}",
                    }
                    for index in range(node_count)
                ],
            }
        }
    )


def test_finish_progress_causal_fence_fails_closed_at_existing_deadline(
    monkeypatch,
    settings,
    workflow_progress_warning_records,
) -> None:
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "WORKFLOW_PROGRESS_TERMINAL_FLUSH_TIMEOUT_SECONDS": 1,
    }
    snapshot = {
        "completed_nodes": 0,
        "failed_nodes": 1,
        "total_nodes": 2,
        "graph": {
            "nodes": [
                {"node_id": "0.0", "state": "PENDING"},
                {"node_id": "0.1", "state": "FAILED"},
            ],
            "edges": [{"source": "0.0", "target": "0.1"}],
        },
        "ingress": {"rejected": 0, "truncated": 0},
    }
    identity = WorkflowRunIdentity(
        task_execution_pk=8,
        attempt_number=1,
        execution_generation=1,
        run_id="terminal-causal-timeout",
    )
    executor = object.__new__(_RayExecutor)
    executor.progress_actor = object()
    executor.workflow_run_identity = identity
    clock = [0.0]
    publications: list[dict[str, Any]] = []

    def flush(**kwargs):
        clock[0] += kwargs["wait_timeout_seconds"]
        return snapshot

    def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(executor, "_flush_progress", flush)
    monkeypatch.setattr(
        executor,
        "_publish_terminal_progress",
        lambda value: publications.append(value) or True,
    )
    monkeypatch.setattr("django_ray.workflows.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("django_ray.workflows.time.sleep", sleep)

    monkeypatch.setattr(
        "django_ray.workflow.progress.publication.publish_unavailable_terminal_workflow_progress",
        lambda *args, **kwargs: True,
    )
    executor.finish_progress(failed=True)

    assert clock[0] == pytest.approx(1.0)
    assert publications == []
    assert executor.progress_actor is None
    assert workflow_progress_warning_records == [
        {
            "message": "Workflow terminal progress did not complete before the flush deadline",
            "component": "workflow_progress",
            "task_execution_pk": 8,
            "workflow_run_id": "terminal-causal-timeout",
            "reason": "snapshot_incomplete",
            "timeout_seconds": 1.0,
            "failed_workflow": True,
        }
    ]


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("ingress_field", ["rejected", "truncated"])
def test_finish_progress_reports_unpublishable_ingress_without_waiting(
    ingress_field,
    failed,
    monkeypatch,
    settings,
) -> None:
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
    }
    snapshot = {
        "completed_nodes": 0,
        "failed_nodes": 0,
        "total_nodes": 2,
        "ingress": {
            "rejected": int(ingress_field == "rejected"),
            "truncated": int(ingress_field == "truncated"),
        },
    }
    publications: list[dict[str, Any]] = []
    sleeps: list[float] = []
    executor = object.__new__(_RayExecutor)
    executor.progress_actor = object()
    executor.workflow_run_identity = _workflow_identity()
    monkeypatch.setattr(executor, "_flush_progress", lambda **kwargs: snapshot)
    monkeypatch.setattr(
        executor,
        "_publish_terminal_progress",
        lambda value: publications.append(value) or False,
    )
    monkeypatch.setattr("django_ray.workflows.time.sleep", sleeps.append)

    executor.finish_progress(failed=failed)

    assert publications == [snapshot]
    assert sleeps == []


def test_finish_progress_reports_rejected_ingress_without_legacy_fallback(
    monkeypatch,
    settings,
) -> None:
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
    }
    snapshots = iter(
        [
            {
                "completed_nodes": 0,
                "failed_nodes": 0,
                "total_nodes": 1,
                "ingress": {"rejected": 1, "truncated": 0},
            },
            {
                "completed_nodes": 1,
                "failed_nodes": 0,
                "total_nodes": 1,
                "ingress": {"rejected": 1, "truncated": 0},
            },
        ]
    )
    sleeps: list[float] = []
    executor = object.__new__(_RayExecutor)
    executor.progress_actor = object()
    executor.workflow_run_identity = None
    monkeypatch.setattr(executor, "_flush_progress", lambda **kwargs: next(snapshots))
    monkeypatch.setattr("django_ray.workflows.time.sleep", sleeps.append)

    executor.finish_progress()

    assert sleeps == []


def test_terminal_publication_is_default_and_failure_is_observational(
    monkeypatch,
    settings,
) -> None:
    import django_ray.workflow.progress.publication as publication_module

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
    }
    publications: list[tuple[object, object, dict[str, Any]]] = []

    def publish(identity, snapshot, **kwargs):
        publications.append((identity, snapshot, kwargs))
        raise AssertionError("fixture publication failure")

    monkeypatch.setattr(
        publication_module,
        "publish_terminal_workflow_progress",
        publish,
    )
    executor = object.__new__(_RayExecutor)
    executor.progress_actor = object()
    executor.workflow_run_identity = _workflow_identity()
    executor._terminal_progress_publication_attempted = False
    snapshot = {"completed_nodes": 1, "failed_nodes": 0, "total_nodes": 1}
    monkeypatch.setattr(executor, "_flush_progress", lambda **kwargs: snapshot)

    executor.finish_progress()

    assert len(publications) == 1
    assert executor._terminal_progress_publication_attempted is True


def test_enabled_terminal_schema_v3_publication_is_attempted_exactly_once(
    monkeypatch,
    settings,
) -> None:
    import django_ray.workflow.progress.publication as publication_module

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "WORKFLOW_PROGRESS_DETAIL_RETENTION_DAYS": 9,
    }
    identity = _workflow_identity()
    snapshot = {"completed_nodes": 1, "failed_nodes": 0, "total_nodes": 1}
    publications: list[tuple[object, object, dict[str, Any]]] = []

    def publish(reported_identity, reported_snapshot, **kwargs):
        publications.append((reported_identity, reported_snapshot, kwargs))
        return publication_module.WorkflowProgressPilotPublicationResult(
            accepted=True,
            reason=publication_module.WorkflowProgressPilotReason.PUBLISHED,
            summary={},
        )

    monkeypatch.setattr(
        publication_module,
        "publish_terminal_workflow_progress",
        publish,
    )
    executor = object.__new__(_RayExecutor)
    executor.progress_actor = object()
    executor.workflow_run_identity = identity
    executor._terminal_progress_publication_attempted = False
    monkeypatch.setattr(executor, "_flush_progress", lambda **kwargs: snapshot)

    executor.finish_progress()
    executor.finish_progress()

    assert publications == [
        (
            identity,
            snapshot,
            {"detail_days": 9},
        )
    ]
    assert executor._terminal_progress_publication_attempted is True


@pytest.mark.parametrize("failed", [False, True])
def test_terminal_only_finish_waits_for_the_outer_task_lifecycle(
    failed,
) -> None:
    executor = object.__new__(_RayExecutor)
    executor.reporting_policy = "terminal_only"
    executor.progress_actor = None
    executor._terminal_progress_publication_attempted = False

    executor.finish_progress(failed=failed)
    executor.finish_progress(failed=failed)

    assert executor.progress_actor is None
    assert executor._terminal_progress_publication_attempted is False


@pytest.mark.parametrize(
    ("failure_mode", "expected_reason"),
    [
        ("rejected", "ingress_rejected"),
        ("exception", "publication_failed"),
    ],
)
def test_terminal_schema_v3_publication_failures_are_bounded_and_best_effort(
    failure_mode,
    expected_reason,
    monkeypatch,
    settings,
    workflow_progress_warning_records,
) -> None:
    import django_ray.workflow.progress.publication as publication_module

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
    }
    identity = _workflow_identity()
    calls = 0

    def publish(*args, **kwargs):
        del args, kwargs
        nonlocal calls
        calls += 1
        if failure_mode == "exception":
            raise RuntimeError("password=do-not-leak")
        return publication_module.WorkflowProgressPilotPublicationResult(
            accepted=False,
            reason=publication_module.WorkflowProgressPilotReason.INGRESS_REJECTED,
        )

    monkeypatch.setattr(
        publication_module,
        "publish_terminal_workflow_progress",
        publish,
    )
    executor = object.__new__(_RayExecutor)
    executor.workflow_run_identity = identity
    executor._terminal_progress_publication_attempted = False

    assert executor._publish_terminal_progress({"state": "SUCCEEDED"}) is False
    assert executor._publish_terminal_progress({"state": "SUCCEEDED"}) is False

    assert calls == 1
    assert workflow_progress_warning_records == [
        {
            "message": "Workflow terminal publication was not completed",
            "component": "workflow_progress",
            "task_execution_pk": identity.task_execution_pk,
            "workflow_run_id": identity.run_id,
            "reason": expected_reason,
        }
    ]
    assert "do-not-leak" not in json.dumps(workflow_progress_warning_records)


@pytest.mark.django_db
def test_schema_v3_pilot_passes_strict_limits_to_the_progress_actor(
    settings,
) -> None:
    from django_ray.models import RayTaskExecution, TaskState
    from django_ray.runtime.context import DurableTaskContext
    from django_ray.workflow.plans import materialize_workflow_plan
    from django_ray.workflow.progress.publication import (
        WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS,
    )

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
    }
    execution = RayTaskExecution.objects.create(
        task_id="workflow-progress-pilot-limits",
        callable_path=f"{increment.__module__}.increment",
        state=TaskState.RUNNING,
        execution_generation=2,
    )
    materialized = materialize_workflow_plan(
        step(increment),
        invocation_args=(1,),
        invocation_kwargs={},
    )
    actor = object()
    actor_calls: list[tuple[bytes, dict[str, Any]]] = []

    class ProgressActor:
        @staticmethod
        def remote(initialized_event: bytes, **kwargs):
            actor_calls.append((initialized_event, kwargs))
            return actor

    executor = object.__new__(_RayExecutor)
    executor.task_context = DurableTaskContext(
        task_pk=execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
    )
    executor.progress_actor = None
    executor.progress_actor_cls = ProgressActor()

    executor.bind_plan(
        materialized,
        requested_policy="auto",
        reporting_policy="full",
    )

    assert executor.progress_actor is actor
    assert executor.workflow_progress_limits is WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS
    assert len(actor_calls) == 1
    initialized_event, actor_kwargs = actor_calls[0]
    assert actor_kwargs == {"limits": WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS}
    assert executor.workflow_run_identity is not None
    decoded = decode_workflow_progress_event(
        initialized_event,
        expected_run_identity=executor.workflow_run_identity.as_dict(),
        limits=WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS,
    )
    assert decoded.kind is WorkflowProgressEventKind.INITIALIZED
    assert decoded.payload == {"plan": materialized.plan.summary()}


@pytest.mark.django_db
def test_terminal_only_bind_claims_plan_without_creating_progress_actor(
    settings,
) -> None:
    from django_ray.models import RayTaskExecution, TaskState
    from django_ray.runtime.context import DurableTaskContext
    from django_ray.workflow.plans import materialize_workflow_plan

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
    }
    execution = RayTaskExecution.objects.create(
        task_id="workflow-terminal-only-bind",
        callable_path=f"{increment.__module__}.increment",
        state=TaskState.RUNNING,
        execution_generation=2,
        progress_data='{"legacy":true}',
    )
    materialized = materialize_workflow_plan(
        step(increment),
        invocation_args=(1,),
        invocation_kwargs={},
    )

    class ProgressActor:
        @staticmethod
        def remote(*args, **kwargs):
            del args, kwargs
            raise AssertionError("terminal-only reporting must not create an actor")

    executor = object.__new__(_RayExecutor)
    executor.task_context = DurableTaskContext(
        task_pk=execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
    )
    executor.progress_actor = None
    executor.progress_actor_cls = ProgressActor()

    executor.bind_plan(
        materialized,
        requested_policy="auto",
        reporting_policy="terminal_only",
    )

    execution.refresh_from_db()
    assert executor.reporting_policy == "terminal_only"
    assert executor.progress_actor is None
    assert executor.workflow_run_identity is not None
    assert execution.progress_data is None
    assert execution.workflow_progress_summary_json is None
    assert execution.workflow_plan_json == materialized.plan.canonical_json
    assert json.loads(execution.workflow_plan_selection)["reporting_policy"] == "terminal_only"


def test_ray_executor_submit_uses_ingest_and_ignores_missing_ray_task_id() -> None:
    remote_calls: list[dict[str, Any]] = []

    class _BadRef:
        def task_id(self):
            raise RuntimeError("task id unavailable")

    class _RemoteStep:
        def options(self, **kwargs):
            return self

        def remote(self, *args, **kwargs):
            remote_calls.append(kwargs)
            return _BadRef(), object()

    identity = _workflow_identity()
    actor = _IngestOnlyProgressActor()
    executor = object.__new__(_RayExecutor)
    executor.task_context = None
    executor.task_execution_pk = identity.task_execution_pk
    executor.workflow_run_identity = identity
    executor.workflow_progress_limits = WORKFLOW_PROGRESS_LIMITS_V1
    executor.progress_actor = actor
    executor.remote_step = _RemoteStep()

    executor.submit_step(step(increment), (), {}, "0.0", ())

    events = _decoded_ingests(actor, identity)
    assert remote_calls == [
        {
            "workflow_run_identity": identity.as_dict(),
            "return_outcome_marker": True,
        }
    ]
    assert [event.kind for event in events] == [WorkflowProgressEventKind.NODE_REGISTERED]
    assert events[0].payload == {
        "callable_path": "tests.unit.test_workflows.increment",
        "label": "increment",
        "node_id": "0.0",
        "ray_options": {},
        "runtime_env": {"mode": "inherit"},
    }


def test_ray_executor_submits_explicit_output_preview_contract() -> None:
    remote_calls: list[dict[str, Any]] = []

    class _BadRef:
        def task_id(self):
            raise RuntimeError("task id unavailable")

    class _RemoteStep:
        def options(self, **kwargs):
            del kwargs
            return self

        def remote(self, *args, **kwargs):
            del args
            remote_calls.append(kwargs)
            return _BadRef(), object()

    identity = _workflow_identity()
    actor = _IngestOnlyProgressActor()
    executor = object.__new__(_RayExecutor)
    executor.task_context = None
    executor.task_execution_pk = identity.task_execution_pk
    executor.workflow_run_identity = identity
    executor.workflow_progress_limits = WORKFLOW_PROGRESS_LIMITS_V1
    executor.progress_actor = actor
    executor.remote_step = _RemoteStep()

    executor.submit_step(
        step(increment).with_output_preview(preview_increment),
        (),
        {},
        "0.0",
        (),
    )

    events = _decoded_ingests(actor, identity)
    assert remote_calls == [
        {
            "output_preview_path": "tests.unit.test_workflows.preview_increment",
            "workflow_run_identity": identity.as_dict(),
            "return_outcome_marker": True,
        }
    ]
    assert [event.kind for event in events] == [
        WorkflowProgressEventKind.NODE_REGISTERED,
        WorkflowProgressEventKind.OUTPUT_PREVIEW,
    ]
    assert events[1].payload == {
        "node_id": "0.0",
        "output_preview": {
            "schema_version": 1,
            "availability": "PENDING",
            "value": None,
        },
    }


def test_terminal_only_submit_omits_all_progress_transport_metadata() -> None:
    remote_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    class _Ref:
        pass

    class _RemoteStep:
        def options(self, **kwargs):
            del kwargs
            return self

        def remote(self, *args, **kwargs):
            remote_calls.append((args, kwargs))
            return _Ref()

    executor = object.__new__(_RayExecutor)
    executor.task_context = None
    executor.task_execution_pk = 1
    executor.workflow_run_identity = _workflow_identity()
    executor.workflow_progress_limits = WORKFLOW_PROGRESS_LIMITS_V1
    executor.reporting_policy = "terminal_only"
    executor.progress_actor = None
    executor.remote_step = _RemoteStep()

    executor.submit_step(
        step(increment).with_output_preview(preview_increment),
        (),
        {},
        "0.0",
        (),
    )

    assert len(remote_calls) == 1
    args, kwargs = remote_calls[0]
    assert args[6] is None
    assert kwargs == {}


def test_strict_ray_executor_submits_exact_nested_workflow_request() -> None:
    from django_ray.execution_codec import (
        ExecutionIdentity,
        NestedCallableBindingKind,
        NestedExecutionBoundaryKind,
        NestedWorkflowBoundaryIdentity,
        decode_nested_execution_request,
    )
    from django_ray.workflow.plans import materialize_workflow_plan

    def thaw(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: thaw(item) for key, item in value.items()}
        if isinstance(value, tuple):
            return [thaw(item) for item in value]
        return value

    remote_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    class _Ref:
        @staticmethod
        def task_id() -> Any:
            raise RuntimeError("task id unavailable")

    class _RemoteStep:
        def options(self, **kwargs: Any) -> _RemoteStep:
            del kwargs
            return self

        def remote(self, *args: Any, **kwargs: Any) -> tuple[_Ref, object]:
            remote_calls.append((args, kwargs))
            return _Ref(), object()

    signature = step(increment).with_output_preview(preview_increment)
    materialized = materialize_workflow_plan(
        signature,
        invocation_args=(1,),
        invocation_kwargs={},
    )
    binding = materialized.binding_for_node("0")
    assert binding is not None
    workflow_identity = _workflow_identity(
        task_execution_pk=42,
        attempt_number=2,
        execution_generation=5,
        run_id="00000000-0000-4000-8000-000000000701",
    )
    executor = object.__new__(_RayExecutor)
    executor.task_context = DurableTaskContext(
        task_pk=42,
        task_id="00000000-0000-4000-8000-000000000042",
        attempt_number=2,
        execution_generation=5,
        execution_protocol_version=1,
        runtime_env_plan_identity=thaw(binding.runtime_env_plan_identity),
        strict_execution_request=True,
    )
    executor.task_execution_pk = 42
    executor.workflow_run_identity = workflow_identity
    executor.workflow_progress_limits = WORKFLOW_PROGRESS_LIMITS_V1
    executor.reporting_policy = "full"
    executor.progress_actor = _IngestOnlyProgressActor()
    executor.materialized_plan = materialized
    executor.remote_step = _RemoteStep()

    executor.submit_step(signature, (1,), {}, "0", ())

    assert len(remote_calls) == 1
    args, kwargs = remote_calls[0]
    expected_outer = ExecutionIdentity(
        task_execution_pk=42,
        task_id="00000000-0000-4000-8000-000000000042",
        attempt_number=2,
        execution_generation=5,
    )
    request = decode_nested_execution_request(
        kwargs["nested_execution_request"],
        expected_outer_identity=expected_outer,
        expected_execution_protocol_version=1,
        expected_boundary_kind=NestedExecutionBoundaryKind.WORKFLOW_STEP,
        expected_boundary_identity=NestedWorkflowBoundaryIdentity(
            workflow_run_id=workflow_identity.run_id,
            node_id="0",
        ),
        expected_callable_binding_kind=NestedCallableBindingKind.PATH,
        expected_callable_binding=f"{increment.__module__}.increment",
        expected_output_preview_callable_path=f"{preview_increment.__module__}.preview_increment",
        expected_runtime_env_plan_digest=binding.runtime_env_plan_digest,
        expected_runtime_env_transport_digest=binding.runtime_env_transport_digest,
    )
    assert args[0] == f"{increment.__module__}.increment"
    assert kwargs["output_preview_path"] == f"{preview_increment.__module__}.preview_increment"
    assert (
        request.output_preview_callable_path == f"{preview_increment.__module__}.preview_increment"
    )
    assert request.runtime_env_plan_identity == thaw(binding.runtime_env_plan_identity)
    assert kwargs["expected_node_id"] == "0"


@pytest.mark.parametrize("strict_marker", [True, False, None, 1])
def test_incomplete_or_malformed_strict_context_never_submits_legacy_work(
    strict_marker: Any,
) -> None:
    from django_ray.execution_codec import (
        NestedExecutionRequestRejected,
        NestedExecutionRequestRejection,
    )
    from django_ray.workflow.plans import materialize_workflow_plan

    class _RemoteStep:
        def options(self, **kwargs: Any) -> _RemoteStep:
            del kwargs
            return self

        def remote(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            pytest.fail("invalid strict context downgraded to a legacy submission")

    signature = step(increment)
    materialized = materialize_workflow_plan(
        signature,
        invocation_args=(1,),
        invocation_kwargs={},
    )
    task_context = DurableTaskContext(task_pk=42)
    object.__setattr__(task_context, "strict_execution_request", strict_marker)
    executor = object.__new__(_RayExecutor)
    executor.task_context = task_context
    executor.task_execution_pk = 42
    executor.workflow_run_identity = _workflow_identity()
    executor.workflow_progress_limits = WORKFLOW_PROGRESS_LIMITS_V1
    executor.reporting_policy = "terminal_only"
    executor.progress_actor = None
    executor.materialized_plan = materialized
    executor.remote_step = _RemoteStep()

    with pytest.raises(NestedExecutionRequestRejected) as captured:
        executor.submit_step(signature, (1,), {}, "0", ())

    assert captured.value.classification is NestedExecutionRequestRejection.MISSING_CONTEXT


def test_ray_executor_submit_passes_strict_pilot_limits_to_remote_step() -> None:
    from django_ray.workflow.progress.limits import (
        WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS,
    )

    class _BadRef:
        def task_id(self):
            raise RuntimeError("task id unavailable")

    remote_calls: list[dict[str, Any]] = []

    class _RemoteStep:
        def options(self, **kwargs):
            return self

        def remote(self, *args, **kwargs):
            remote_calls.append(kwargs)
            return _BadRef(), object()

    identity = _workflow_identity()
    executor = object.__new__(_RayExecutor)
    executor.task_context = None
    executor.task_execution_pk = identity.task_execution_pk
    executor.workflow_run_identity = identity
    executor.workflow_progress_limits = WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS
    executor.progress_actor = _IngestOnlyProgressActor()
    executor.remote_step = _RemoteStep()

    executor.submit_step(step(increment), (), {}, "0.0", ())

    assert remote_calls == [
        {
            "workflow_progress_limits": WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS,
            "workflow_run_identity": identity.as_dict(),
            "return_outcome_marker": True,
        }
    ]


def test_ray_executor_submit_chunks_edges_and_uses_only_bounded_ingest() -> None:
    class _TaskId:
        @staticmethod
        def hex() -> str:
            return "ray-task-217"

    class _GoodRef:
        @staticmethod
        def task_id() -> _TaskId:
            return _TaskId()

    class _RemoteStep:
        def options(self, **kwargs):
            return self

        def remote(self, *args, **kwargs):
            return _GoodRef(), object()

    identity = _workflow_identity()
    actor = _IngestOnlyProgressActor()
    executor = object.__new__(_RayExecutor)
    executor.task_context = None
    executor.task_execution_pk = identity.task_execution_pk
    executor.workflow_run_identity = identity
    executor.workflow_progress_limits = WORKFLOW_PROGRESS_LIMITS_V1
    executor.progress_actor = actor
    executor.remote_step = _RemoteStep()
    dependencies = tuple(f"0.upstream-{index}" for index in range(65))

    executor.submit_step(step(increment), (), {}, "0.1", dependencies)

    events = _decoded_ingests(actor, identity)
    assert [event.kind for event in events] == [
        WorkflowProgressEventKind.NODE_REGISTERED,
        WorkflowProgressEventKind.EDGES_REGISTERED,
        WorkflowProgressEventKind.EDGES_REGISTERED,
        WorkflowProgressEventKind.EDGES_REGISTERED,
        WorkflowProgressEventKind.SUBMITTED,
    ]
    edge_batches = [
        event.payload["edges"]
        for event in events
        if event.kind is WorkflowProgressEventKind.EDGES_REGISTERED
    ]
    assert [len(edges) for edges in edge_batches] == [32, 32, 1]
    assert [edge for batch in edge_batches for edge in batch] == [
        {"source": dependency, "target": "0.1"} for dependency in dependencies
    ]
    assert events[-1].payload == {
        "label": "increment",
        "node_id": "0.1",
        "ray_task_id": "ray-task-217",
    }


def test_ray_executor_invalid_internal_progress_never_calls_actor() -> None:
    class _TaskId:
        @staticmethod
        def hex() -> str:
            return "ray-task-invalid"

    class _RefWithTaskId:
        @staticmethod
        def task_id() -> _TaskId:
            return _TaskId()

    class _RemoteStep:
        calls = 0

        def options(self, **kwargs):
            return self

        def remote(self, *args, **kwargs):
            self.calls += 1
            return _RefWithTaskId(), object()

    identity = _workflow_identity()
    actor = _IngestOnlyProgressActor()
    remote_step = _RemoteStep()
    executor = object.__new__(_RayExecutor)
    executor.task_context = None
    executor.task_execution_pk = identity.task_execution_pk
    executor.workflow_run_identity = identity
    executor.workflow_progress_limits = WORKFLOW_PROGRESS_LIMITS_V1
    executor.progress_actor = actor
    executor.remote_step = remote_step

    executor.submit_step(step(increment), (), {}, "x" * 257, ())

    assert remote_step.calls == 1
    assert actor.ingest.calls == []


@pytest.mark.parametrize(
    ("failed", "terminal_kind"),
    [
        (False, WorkflowProgressEventKind.COMPLETED),
        (True, WorkflowProgressEventKind.FAILED),
    ],
)
def test_ray_executor_map_lifecycle_uses_only_bounded_ingest(
    failed: bool,
    terminal_kind: WorkflowProgressEventKind,
) -> None:
    identity = _workflow_identity()
    actor = _IngestOnlyProgressActor()
    executor = object.__new__(_RayExecutor)
    executor.progress_actor = actor
    executor.workflow_run_identity = identity
    executor.workflow_progress_limits = WORKFLOW_PROGRESS_LIMITS_V1
    executor._progress_suppression_depth = 0
    executor._map_progress_sent_at = {}
    executor._map_progress_producers = {}
    dependencies = tuple(f"0.upstream-{index}" for index in range(65))

    executor.map_started(
        "0.1",
        "map:increment",
        dependencies,
        max_concurrency=2,
        max_items=100,
    )
    executor.map_progress(
        "0.1",
        "map:increment",
        submitted=4,
        completed=2,
        input_exhausted=False,
        force=True,
    )
    executor.map_finished(
        "0.1",
        "map:increment",
        submitted=4,
        completed=4 if not failed else 2,
        input_exhausted=True,
        failed=failed,
        error="bounded map failed" if failed else None,
    )

    events = _decoded_ingests(actor, identity)
    assert [event.kind for event in events] == [
        WorkflowProgressEventKind.MAP_REGISTERED,
        WorkflowProgressEventKind.EDGES_REGISTERED,
        WorkflowProgressEventKind.EDGES_REGISTERED,
        WorkflowProgressEventKind.EDGES_REGISTERED,
        WorkflowProgressEventKind.MAP_PROGRESS,
        WorkflowProgressEventKind.MAP_PROGRESS,
        WorkflowProgressEventKind.PRODUCER_REPORT,
        terminal_kind,
    ]
    assert [
        len(event.payload["edges"])
        for event in events
        if event.kind is WorkflowProgressEventKind.EDGES_REGISTERED
    ] == [32, 32, 1]
    assert events[-3].payload == {
        "completed": 4 if not failed else 2,
        "input_exhausted": True,
        "label": "map:increment",
        "node_id": "0.1",
        "submitted": 4,
    }
    expected_terminal = {
        "label": "map:increment",
        "node_id": "0.1",
    }
    if failed:
        expected_terminal["error"] = "bounded map failed"
    assert events[-1].payload == expected_terminal
    assert events[-2].payload["offered"] == 2
    executor.map_finished(
        "0.1",
        "map:increment",
        submitted=4,
        completed=4 if not failed else 2,
        input_exhausted=True,
        failed=failed,
    )
    assert _decoded_ingests(actor, identity) == events


def test_progress_actor_requires_complete_run_identity() -> None:
    executor = object.__new__(_RayExecutor)
    executor.progress_actor = _IngestOnlyProgressActor()
    executor.workflow_run_identity = None
    executor._progress_suppression_depth = 0
    executor._map_progress_sent_at = {}
    executor._map_progress_producers = {}

    with pytest.raises(AssertionError, match="complete run identity"):
        executor.map_started(
            "0.1",
            "map:increment",
            (),
            max_concurrency=2,
            max_items=10,
        )


def test_ray_map_cleanup_has_a_hard_deadline() -> None:
    class _SlowRay:
        def __init__(self) -> None:
            self.cancelled: list[object] = []
            self.wait_timeouts: list[float] = []
            self.get_calls = 0

        def cancel(self, ref, *, force, recursive):
            assert force is False
            assert recursive is True
            self.cancelled.append(ref)

        def wait(self, refs, *, num_returns, timeout, fetch_local=True):
            assert num_returns == len(refs)
            if timeout == 0:
                assert fetch_local is False
                return [], refs
            self.wait_timeouts.append(timeout)
            time.sleep(timeout)
            return [], refs

        def get(self, ref):
            del ref
            self.get_calls += 1

    ray = _SlowRay()
    executor = object.__new__(_RayExecutor)
    executor.ray = ray
    refs = [object(), object()]
    started_at = time.monotonic()

    executor.cancel_and_drain(refs, timeout_seconds=0.02)

    elapsed = time.monotonic() - started_at
    assert ray.cancelled == refs
    assert ray.wait_timeouts == [0.02]
    assert ray.get_calls == 0
    assert elapsed < 0.1


def test_ray_map_cleanup_skips_cancelling_already_ready_refs_but_drains_them() -> None:
    ready_ref = object()
    running_ref = object()

    class _MixedReadinessRay:
        def __init__(self) -> None:
            self.cancelled: list[object] = []
            self.drained: list[object] = []

        def cancel(self, ref, *, force, recursive):
            assert force is False
            assert recursive is True
            self.cancelled.append(ref)

        def wait(self, refs, *, num_returns, timeout, fetch_local=True):
            assert num_returns == len(refs)
            if timeout == 0:
                assert fetch_local is False
                return [ready_ref], [running_ref]
            return list(refs), []

        def get(self, ref):
            self.drained.append(ref)

    ray = _MixedReadinessRay()
    executor = object.__new__(_RayExecutor)
    executor.ray = ray

    executor.cancel_and_drain([ready_ref, running_ref], timeout_seconds=0.05)

    assert ray.cancelled == [running_ref]
    assert ray.drained == [ready_ref, running_ref]


@pytest.mark.django_db
def test_ray_executor_flushes_failed_progress_snapshot() -> None:
    from django_ray.models import RayTaskExecution
    from django_ray.workflow.progress.runs import allocate_workflow_run

    execution = RayTaskExecution.objects.create(
        task_id="workflow-flush",
        callable_path="tests.unit.test_workflows.increment",
        state="RUNNING",
        attempt_number=2,
        execution_generation=4,
    )
    identity = allocate_workflow_run(
        DurableTaskContext(
            task_pk=execution.pk,
            attempt_number=2,
            execution_generation=4,
        )
    )
    assert identity is not None
    snapshot_ref = object()
    snapshot = {
        "schema_version": WORKFLOW_PROGRESS_SCHEMA_VERSION,
        "run_identity": identity.as_dict(),
        "revision": 2,
        "state": "RUNNING",
        "completed_nodes": 0,
        "failed_nodes": 1,
        "total_nodes": 1,
    }
    executor = object.__new__(_RayExecutor)
    executor.progress_actor = SimpleNamespace(snapshot=SimpleNamespace(remote=lambda: snapshot_ref))
    executor.task_execution_pk = execution.pk
    executor.workflow_run_identity = identity
    executor.last_progress_revision = 2
    executor.ray = SimpleNamespace(
        wait=lambda refs, timeout: (refs, []),
        get=lambda ref: snapshot,
    )

    flushed = executor._flush_progress(failed=True)
    assert flushed is not None
    assert flushed["state"] == "FAILED"

    execution.refresh_from_db()
    assert execution.progress_data is None


@pytest.mark.django_db
def test_ray_executor_does_not_write_legacy_snapshot_after_cancellation(
    monkeypatch,
    workflow_progress_warning_records,
) -> None:
    from django_ray.models import RayTaskExecution, TaskState
    from django_ray.workflow.progress.runs import allocate_workflow_run

    execution = RayTaskExecution.objects.create(
        task_id="workflow-stale-flush",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=2,
    )
    identity = allocate_workflow_run(
        DurableTaskContext(
            task_pk=execution.pk,
            attempt_number=1,
            execution_generation=2,
        )
    )
    assert identity is not None
    RayTaskExecution.objects.filter(pk=execution.pk).update(state=TaskState.CANCELLED)

    snapshot = {
        "schema_version": WORKFLOW_PROGRESS_SCHEMA_VERSION,
        "run_identity": identity.as_dict(),
        "revision": 1,
        "state": "RUNNING",
        "completed_nodes": 0,
        "failed_nodes": 0,
        "total_nodes": 1,
    }
    disabled: list[bool] = []
    snapshot_ref = object()
    actor = SimpleNamespace(
        snapshot=SimpleNamespace(remote=lambda: snapshot_ref),
        disable=SimpleNamespace(remote=lambda: disabled.append(True)),
    )
    executor = object.__new__(_RayExecutor)
    executor.progress_actor = actor
    executor.task_execution_pk = execution.pk
    executor.workflow_run_identity = identity
    executor.last_progress_revision = -1
    executor.ray = SimpleNamespace(
        wait=lambda refs, timeout: (refs, []),
        get=lambda ref: snapshot,
    )
    sleeps: list[float] = []
    monkeypatch.setattr("django_ray.workflows.time.sleep", sleeps.append)

    assert executor._flush_progress(bypass_interval=True) == snapshot

    execution.refresh_from_db()
    assert execution.state == TaskState.CANCELLED
    assert execution.progress_data is None
    assert disabled == []
    assert sleeps == []
    assert workflow_progress_warning_records == []
