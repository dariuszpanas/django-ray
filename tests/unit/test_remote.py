"""Tests for module-level Ray task and workflow executors."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import django_ray.runtime.remote as remote_module
from django_ray.runtime.remote import (
    WorkflowProgressActor,
    execute_django_task_remote,
    execute_workflow_step_remote,
)
from django_ray.workflow_progress_protocol import (
    WORKFLOW_PROGRESS_LIMITS_V1,
    WorkflowProgressEventKind,
    WorkflowProgressLimits,
    WorkflowProgressProtocolError,
    decode_workflow_progress_event,
    prepare_workflow_progress_event,
)


class _RemoteMethod:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def remote(self, *args) -> None:
        self.calls.append(args)


class _ProgressActor:
    def __init__(self) -> None:
        self.ingest = _RemoteMethod()


_WORKFLOW_RUN_IDENTITY = {
    "schema_version": 1,
    "run_id": "00000000-0000-0000-0000-000000000217",
    "task_execution_pk": 9,
    "attempt_number": 2,
    "execution_generation": 6,
}
_WORKFLOW_PLAN = {
    "plan_format": "django-ray.workflow-plan",
    "plan_format_version": 1,
    "fingerprint": f"sha256:{'a' * 64}",
    "definition_name": "workflow:test",
    "definition_revision": f"sha256:{'b' * 64}",
    "topology_class": "static",
    "node_count": 2,
}
_OCCURRED_AT = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def _progress_wire(
    kind: WorkflowProgressEventKind,
    payload: dict[str, object],
    *,
    run_identity: dict[str, object] | None = None,
) -> bytes:
    return prepare_workflow_progress_event(
        _WORKFLOW_RUN_IDENTITY if run_identity is None else run_identity,
        kind,
        payload,
        occurred_at=_OCCURRED_AT,
    )


def _progress_actor(
    *,
    limits: WorkflowProgressLimits = WORKFLOW_PROGRESS_LIMITS_V1,
) -> WorkflowProgressActor:
    return WorkflowProgressActor(
        _progress_wire(
            WorkflowProgressEventKind.INITIALIZED,
            {"plan": _WORKFLOW_PLAN},
        ),
        limits=limits,
    )


def workflow_target(value: int, increment: int = 0) -> int:
    return value + increment


def test_task_bootstrap_unpickles_before_django_ray_is_importable() -> None:
    import ray.cloudpickle as cloudpickle

    cloudpickle.register_pickle_by_value(remote_module)
    try:
        payload = cloudpickle.dumps(execute_django_task_remote)
    finally:
        cloudpickle.unregister_pickle_by_value(remote_module)

    unpickle_without_django_ray = """
import importlib.abc
import sys

import ray.cloudpickle as cloudpickle


class BlockDjangoRay(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "django_ray" or fullname.startswith("django_ray."):
            raise ModuleNotFoundError(fullname, name=fullname)
        return None


sys.meta_path.insert(0, BlockDjangoRay())
cloudpickle.loads(sys.stdin.buffer.read())
"""
    result = subprocess.run(
        [sys.executable, "-c", unpickle_without_django_ray],
        input=payload,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def failing_workflow_target(value: int) -> int:
    raise RuntimeError(f"failed:{value}")


def workflow_context_target() -> dict[str, object] | None:
    from django_ray.runtime.context import get_current_workflow_run_identity

    return get_current_workflow_run_identity()


def test_execute_django_task_remote_logs_failure(monkeypatch, capsys) -> None:
    payload = json.dumps({"success": False, "error": "boom"})
    monkeypatch.setattr("django_ray.runtime.entrypoint.execute_task", lambda *args: payload)

    result = execute_django_task_remote("tests.fake", "[]", "{}", 12)

    captured = capsys.readouterr()
    assert result == payload
    assert "[Task 12] Starting: tests.fake" in captured.out
    assert "[Task 12] FAILED: boom" in captured.err


def test_execute_django_task_remote_logs_bounded_success_metadata(monkeypatch, capsys) -> None:
    payload = json.dumps({"success": True, "result": {"password": "secret-value"}})
    monkeypatch.setattr("django_ray.runtime.entrypoint.execute_task", lambda *args: payload)

    execute_django_task_remote("tests.fake", "[]", "{}", 13)

    captured = capsys.readouterr()
    assert "SUCCESS" in captured.out
    assert "secret-value" not in captured.out
    assert "result_type" in captured.out


def test_execute_django_task_remote_forwards_input_reference(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_execute(*args, **kwargs) -> str:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return json.dumps({"success": True, "result": None})

    monkeypatch.setattr("django_ray.runtime.entrypoint.execute_task", fake_execute)

    execute_django_task_remote(
        "tests.fake",
        "null",
        "null",
        14,
        input_reference="resultfs://input",
    )

    assert captured["kwargs"] == {"input_reference": "resultfs://input"}


def test_execute_django_task_remote_propagates_attempt_and_generation(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_execute(*_args, **_kwargs) -> str:
        from django_ray.runtime.context import get_current_task_context

        context = get_current_task_context()
        assert context is not None
        captured.update(
            task_pk=context.task_pk,
            attempt_number=context.attempt_number,
            execution_generation=context.execution_generation,
            compiled_graph_submission_transport=(context.compiled_graph_submission_transport),
        )
        return json.dumps({"success": True, "result": None})

    monkeypatch.setattr("django_ray.runtime.entrypoint.execute_task", fake_execute)

    execute_django_task_remote(
        "tests.fake",
        "[]",
        "{}",
        15,
        attempt_number=3,
        execution_generation=9,
        compiled_graph_submission_transport="ray-client",
    )

    assert captured == {
        "task_pk": 15,
        "attempt_number": 3,
        "execution_generation": 9,
        "compiled_graph_submission_transport": "ray-client",
    }


def test_execute_workflow_step_bootstraps_and_reports_completion(monkeypatch) -> None:
    bootstrapped: list[bool] = []
    monkeypatch.setattr(
        "django_ray.runtime.entrypoint.bootstrap_django",
        lambda: bootstrapped.append(True),
    )
    monkeypatch.setattr(remote_module, "_ray_execution_metadata", lambda: {"ray_task_id": "1"})
    actor = _ProgressActor()
    log_context: dict[str, object] = {}
    monkeypatch.setattr(
        "django_ray.logging.get_logger",
        lambda _name, **kwargs: (
            log_context.update(kwargs)
            or SimpleNamespace(info=lambda *_args: None, exception=lambda *_args: None)
        ),
    )
    run_identity = dict(_WORKFLOW_RUN_IDENTITY)

    result = execute_workflow_step_remote(
        "tests.unit.test_remote.workflow_target",
        True,
        (),
        {"increment": 2},
        {},
        9,
        actor,
        "0.0",
        3,
        workflow_run_identity=run_identity,
    )

    assert result == 5
    assert bootstrapped == [True]
    assert len(actor.ingest.calls) == 2
    events = [
        decode_workflow_progress_event(
            call[0],
            expected_run_identity=run_identity,
        )
        for call in actor.ingest.calls
    ]
    assert [event.kind for event in events] == [
        WorkflowProgressEventKind.STARTED,
        WorkflowProgressEventKind.COMPLETED,
    ]
    assert events[0].payload == {
        "execution": {
            "assigned_resources": {},
            "ray_job_id": None,
            "ray_node_id": None,
            "ray_task_id": "1",
            "ray_worker_id": None,
        },
        "label": "workflow_target",
        "node_id": "0.0",
    }
    assert events[1].payload == {
        "label": "workflow_target",
        "node_id": "0.0",
    }
    assert all(event.run_identity == run_identity for event in events)
    assert log_context["workflow_run_id"] == run_identity["run_id"]
    assert log_context["workflow_attempt_number"] == 2
    assert log_context["workflow_execution_generation"] == 6


def test_execute_workflow_step_exposes_run_identity_to_leaf(monkeypatch) -> None:
    from django_ray.runtime.context import get_current_workflow_run_identity

    monkeypatch.setattr(remote_module, "_ray_execution_metadata", dict)
    run_identity = {
        "schema_version": 1,
        "run_id": "00000000-0000-0000-0000-000000000012",
        "task_execution_pk": 10,
        "attempt_number": 4,
        "execution_generation": 8,
    }

    result = execute_workflow_step_remote(
        "tests.unit.test_remote.workflow_context_target",
        False,
        (),
        {},
        {},
        10,
        None,
        "0.0",
        workflow_run_identity=run_identity,
    )

    assert result == run_identity
    assert get_current_workflow_run_identity() is None


def test_execute_workflow_step_reports_failure() -> None:
    import ray

    assert not ray.is_initialized()
    actor = _ProgressActor()
    run_identity = dict(_WORKFLOW_RUN_IDENTITY)

    with pytest.raises(RuntimeError, match="failed:3"):
        execute_workflow_step_remote(
            "tests.unit.test_remote.failing_workflow_target",
            False,
            (),
            {},
            {},
            None,
            actor,
            "0.1",
            3,
            workflow_run_identity=run_identity,
        )

    events = [
        decode_workflow_progress_event(
            call[0],
            expected_run_identity=run_identity,
        )
        for call in actor.ingest.calls
    ]
    assert [event.kind for event in events] == [
        WorkflowProgressEventKind.STARTED,
        WorkflowProgressEventKind.FAILED,
    ]
    assert events[-1].payload == {
        "error": "failed:3",
        "label": "failing_workflow_target",
        "node_id": "0.1",
    }
    assert not ray.is_initialized()


def test_execute_workflow_step_ignores_progress_reporting_failures() -> None:
    actor = _ProgressActor()

    def reject_wire(_wire: bytes) -> None:
        raise RuntimeError("progress actor unavailable")

    actor.ingest.remote = reject_wire

    result = execute_workflow_step_remote(
        "tests.unit.test_remote.workflow_target",
        False,
        (),
        {"increment": 2},
        {},
        9,
        actor,
        "0.0",
        3,
        workflow_run_identity=dict(_WORKFLOW_RUN_IDENTITY),
    )

    assert result == 5


def test_ray_execution_metadata_handles_context_and_runtime_errors(monkeypatch) -> None:
    context = SimpleNamespace(
        get_task_id=lambda: "task",
        get_job_id=lambda: "job",
        get_node_id=lambda: "node",
        get_worker_id=lambda: "worker",
        get_assigned_resources=lambda: {"CPU": 1},
    )
    monkeypatch.setitem(
        sys.modules,
        "ray",
        SimpleNamespace(
            is_initialized=lambda: True,
            get_runtime_context=lambda: context,
        ),
    )

    assert remote_module._ray_execution_metadata() == {
        "ray_task_id": "task",
        "ray_job_id": "job",
        "ray_node_id": "node",
        "ray_worker_id": "worker",
        "assigned_resources": {"CPU": 1},
    }

    monkeypatch.setitem(
        sys.modules,
        "ray",
        SimpleNamespace(
            is_initialized=lambda: True,
            get_runtime_context=lambda: (_ for _ in ()).throw(RuntimeError("outside Ray")),
        ),
    )
    assert remote_module._ray_execution_metadata() == {}


def test_ray_execution_metadata_skips_context_when_ray_is_uninitialized(
    monkeypatch,
) -> None:
    context_lookups: list[bool] = []
    monkeypatch.setitem(
        sys.modules,
        "ray",
        SimpleNamespace(
            is_initialized=lambda: False,
            get_runtime_context=lambda: context_lookups.append(True),
        ),
    )

    assert remote_module._ray_execution_metadata() == {}
    assert context_lookups == []


def test_ray_execution_metadata_does_not_import_ray() -> None:
    probe = """
import json
import sys

ray_loaded_before = "ray" in sys.modules
from django_ray.runtime.remote import _ray_execution_metadata

payload = {
    "ray_loaded_before": ray_loaded_before,
    "ray_loaded_after_module_import": "ray" in sys.modules,
    "metadata": _ray_execution_metadata(),
    "ray_loaded_after_metadata": "ray" in sys.modules,
}
print("DJANGO_RAY_COLD_METADATA_PROBE=" + json.dumps(payload, sort_keys=True))
"""

    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    marker = "DJANGO_RAY_COLD_METADATA_PROBE="
    payload_line = next(line for line in result.stdout.splitlines() if line.startswith(marker))
    assert json.loads(payload_line.removeprefix(marker)) == {
        "metadata": {},
        "ray_loaded_after_metadata": False,
        "ray_loaded_after_module_import": False,
        "ray_loaded_before": False,
    }


def test_ray_execution_metadata_does_not_initialize_local_ray() -> None:
    probe = """
import json

import ray

from django_ray.runtime.remote import _ray_execution_metadata

payload = {
    "initialized_before": ray.is_initialized(),
    "metadata": _ray_execution_metadata(),
    "initialized_after": ray.is_initialized(),
}
print("DJANGO_RAY_METADATA_PROBE=" + json.dumps(payload, sort_keys=True))
"""
    environment = os.environ.copy()
    environment.pop("RAY_ADDRESS", None)

    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    marker = "DJANGO_RAY_METADATA_PROBE="
    payload_line = next(line for line in result.stdout.splitlines() if line.startswith(marker))
    assert json.loads(payload_line.removeprefix(marker)) == {
        "initialized_after": False,
        "initialized_before": False,
        "metadata": {},
    }
    combined_output = f"{result.stdout}\n{result.stderr}".lower()
    assert "local ray instance" not in combined_output


def test_progress_actor_ingests_bounded_events_and_preserves_terminal_state() -> None:
    actor = _progress_actor()
    events = [
        _progress_wire(
            WorkflowProgressEventKind.EDGES_REGISTERED,
            {"edges": [{"source": "0.0", "target": "0.1"}]},
        ),
        _progress_wire(
            WorkflowProgressEventKind.NODE_REGISTERED,
            {
                "node_id": "0.0",
                "label": "first",
                "callable_path": "tests.unit.test_remote.workflow_target",
                "runtime_env": {"mode": "inherit"},
                "ray_options": {"num_cpus": 1},
            },
        ),
        _progress_wire(
            WorkflowProgressEventKind.NODE_REGISTERED,
            {
                "node_id": "0.1",
                "label": "second",
                "callable_path": "tests.unit.test_remote.workflow_target",
                "runtime_env": {"mode": "inherit"},
                "ray_options": {},
            },
        ),
        _progress_wire(
            WorkflowProgressEventKind.STARTED,
            {
                "node_id": "0.0",
                "label": "first",
                "execution": {"ray_task_id": "task-1"},
            },
        ),
        _progress_wire(
            WorkflowProgressEventKind.APPLICATION_PROGRESS,
            {
                "node_id": "0.0",
                "current": 1.0,
                "total": 2.0,
                "message": "half",
                "metrics": {"rows": 1},
            },
        ),
        _progress_wire(
            WorkflowProgressEventKind.COMPLETED,
            {"node_id": "0.0", "label": "first"},
        ),
        _progress_wire(
            WorkflowProgressEventKind.STARTED,
            {
                "node_id": "0.0",
                "label": "late",
                "execution": {"ray_task_id": "task-late"},
            },
        ),
        _progress_wire(
            WorkflowProgressEventKind.FAILED,
            {"node_id": "0.1", "label": "second", "error": "bounded failure"},
        ),
    ]

    assert all(actor.ingest(event) for event in events)

    snapshot = actor.snapshot()
    first, second = snapshot["graph"]["nodes"]
    assert snapshot["schema_version"] == 2
    assert snapshot["run_identity"] == _WORKFLOW_RUN_IDENTITY
    assert snapshot["plan"] == _WORKFLOW_PLAN
    assert snapshot["state"] == "FAILED"
    assert snapshot["graph"]["edges"] == [{"source": "0.0", "target": "0.1"}]
    assert first["dependencies"] == []
    assert first["state"] == "SUCCEEDED"
    assert first["label"] == "late"
    assert first["execution"]["ray_task_id"] == "task-late"
    assert first["progress"]["current"] == 2.0
    assert first["progress"]["percent"] == 100.0
    assert first["started_at"] == _OCCURRED_AT.timestamp()
    assert first["finished_at"] == _OCCURRED_AT.timestamp()
    assert second["dependencies"] == ["0.0"]
    assert second["state"] == "FAILED"
    assert second["error"] == "bounded failure"
    assert [event["event"] for event in snapshot["recent_events"]] == [
        "STARTED",
        "PROGRESS",
        "COMPLETED",
        "FAILED",
    ]
    assert snapshot["ingress"]["accepted"] == 1 + len(events)
    assert snapshot["ingress"]["rejected"] == 0
    assert snapshot["ingress"]["retained_nodes"] == 2
    assert snapshot["ingress"]["retained_edges"] == 1


def test_progress_actor_requires_one_canonical_initialization_event() -> None:
    with pytest.raises(WorkflowProgressProtocolError, match="requires an initialized event"):
        WorkflowProgressActor(
            _progress_wire(
                WorkflowProgressEventKind.COMPLETED,
                {"node_id": "not-initialized", "label": "not-initialized"},
            )
        )
    with pytest.raises(WorkflowProgressProtocolError):
        WorkflowProgressActor(b'{"invalid":"initialization"}')
    legacy_constructor: Any = WorkflowProgressActor
    with pytest.raises(TypeError):
        legacy_constructor(
            task_execution_pk=9,
            attempt_number=2,
            execution_generation=6,
            workflow_run_id=_WORKFLOW_RUN_IDENTITY["run_id"],
            plan_summary=_WORKFLOW_PLAN,
        )


def test_progress_actor_ray_surface_exposes_only_ingest_and_controls() -> None:
    import ray

    assert not ray.is_initialized()
    remote_actor = ray.remote(num_cpus=0)(WorkflowProgressActor)
    metadata: Any = remote_actor.__ray_metadata__
    ray_internal_methods = {
        "__init__",
        "__ray_call__",
        "__ray_ready__",
        "__ray_terminate__",
    }

    assert set(metadata.method_meta.methods) - ray_internal_methods == {
        "ingest",
        "disable",
        "snapshot",
    }
    assert not ray.is_initialized()


def test_progress_actor_tolerates_out_of_order_node_placeholders() -> None:
    actor = _progress_actor()
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.MAP_PROGRESS,
            {
                "node_id": "map-first",
                "label": "map:first",
                "submitted": 3,
                "completed": 1,
                "input_exhausted": False,
            },
        )
    )
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.COMPLETED,
            {"node_id": "terminal-first", "label": "terminal:first"},
        )
    )
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.STARTED,
            {
                "node_id": "terminal-first",
                "label": "late-start",
                "execution": {"ray_task_id": "late-task"},
            },
        )
    )
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.SUBMITTED,
            {
                "node_id": "terminal-first",
                "label": "late-submitted",
                "ray_task_id": "late-submitted-task",
            },
        )
    )
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.NODE_REGISTERED,
            {
                "node_id": "terminal-first",
                "label": "registered-late",
                "callable_path": "tests.unit.test_remote.workflow_target",
                "runtime_env": {"mode": "inherit"},
                "ray_options": {},
            },
        )
    )

    map_node, terminal_node = actor.snapshot()["graph"]["nodes"]
    assert map_node["node_id"] == "map-first"
    assert map_node["kind"] == "map"
    assert map_node["state"] == "RUNNING"
    assert map_node["fanout"]["in_flight_items"] == 2
    assert terminal_node["node_id"] == "terminal-first"
    assert terminal_node["state"] == "SUCCEEDED"
    assert terminal_node["label"] == "registered-late"
    assert terminal_node["execution"]["ray_task_id"] == "late-submitted-task"
    assert [event["event"] for event in actor.snapshot()["recent_events"]] == [
        "PROGRESS",
        "COMPLETED",
    ]


def test_progress_actor_rejects_before_mutation_and_exposes_no_legacy_rpc() -> None:
    actor = _progress_actor()
    baseline = actor.snapshot()
    wrong_identity = {
        **_WORKFLOW_RUN_IDENTITY,
        "run_id": "00000000-0000-0000-0000-000000000218",
    }

    assert actor.ingest(b'{"secret":"rejected-value"}') is False
    assert (
        actor.ingest(
            _progress_wire(
                WorkflowProgressEventKind.FAILED,
                {
                    "node_id": "wrong-fence",
                    "label": "wrong",
                    "error": "wrong-fence-secret",
                },
                run_identity=wrong_identity,
            )
        )
        is False
    )
    assert (
        actor.ingest(
            _progress_wire(
                WorkflowProgressEventKind.INITIALIZED,
                {"plan": _WORKFLOW_PLAN},
            )
        )
        is False
    )

    snapshot = actor.snapshot()
    assert snapshot["revision"] == baseline["revision"]
    assert snapshot["updated_at"] == baseline["updated_at"]
    assert snapshot["graph"] == baseline["graph"]
    assert snapshot["recent_events"] == baseline["recent_events"]
    assert snapshot["ingress"]["retained_bytes"] == baseline["ingress"]["retained_bytes"]
    assert snapshot["ingress"]["rejected"] == 3
    assert snapshot["ingress"]["rejected_by_reason"]["protocol_error"] == 1
    assert snapshot["ingress"]["rejected_by_reason"]["fence_mismatch"] == 1
    assert snapshot["ingress"]["rejected_by_reason"]["unexpected_initialized"] == 1
    assert set(snapshot["ingress"]["rejected_by_reason"]) == {
        "protocol_error",
        "fence_mismatch",
        "unexpected_initialized",
        "node_limit",
        "edge_limit",
        "retained_bytes_limit",
    }
    assert "rejected-value" not in json.dumps(snapshot)
    assert "wrong-fence-secret" not in json.dumps(snapshot)
    assert {name for name, value in vars(WorkflowProgressActor).items() if callable(value)} == {
        "__init__",
        "ingest",
        "disable",
        "snapshot",
    }
    for legacy_method in (
        "register",
        "register_map",
        "submitted",
        "started",
        "progress",
        "map_progress",
        "completed",
        "failed",
    ):
        assert not hasattr(actor, legacy_method)


def test_progress_actor_replacement_subtracts_prior_canonical_size() -> None:
    actor = _progress_actor()
    long_label = "long-" + ("x" * 200)
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.NODE_REGISTERED,
            {
                "node_id": "replace",
                "label": long_label,
                "callable_path": "tests.unit.test_remote.workflow_target",
                "runtime_env": {"mode": "inherit"},
                "ray_options": {},
            },
        )
    )
    retained_with_long_label = actor.snapshot()["ingress"]["retained_bytes"]

    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.NODE_REGISTERED,
            {
                "node_id": "replace",
                "label": "short",
                "callable_path": "tests.unit.test_remote.workflow_target",
                "runtime_env": {"mode": "inherit"},
                "ray_options": {},
            },
        )
    )

    snapshot = actor.snapshot()
    retained_state = {
        "edges": snapshot["graph"]["edges"],
        "nodes": snapshot["graph"]["nodes"],
        "plan": snapshot["plan"],
        "recent_events": snapshot["recent_events"],
    }
    assert snapshot["ingress"]["retained_nodes"] == 1
    assert snapshot["ingress"]["retained_bytes"] < retained_with_long_label
    assert snapshot["ingress"]["retained_bytes"] == len(
        json.dumps(
            retained_state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )
    assert snapshot["graph"]["nodes"][0]["label"] == "short"


def test_progress_actor_rejects_cardinality_overflow_with_fixed_reasons() -> None:
    limits = replace(
        WORKFLOW_PROGRESS_LIMITS_V1,
        topology_node_max_items=1,
        topology_edge_max_items=1,
        recent_event_max_items=2,
        combined_max_decoded_bytes=4096,
    )
    actor = _progress_actor(limits=limits)
    node_payload = {
        "node_id": "bounded",
        "label": "bounded",
        "callable_path": "tests.unit.test_remote.workflow_target",
        "runtime_env": {"mode": "inherit"},
        "ray_options": {},
    }

    assert actor.ingest(_progress_wire(WorkflowProgressEventKind.NODE_REGISTERED, node_payload))
    assert (
        actor.ingest(
            _progress_wire(
                WorkflowProgressEventKind.NODE_REGISTERED,
                {**node_payload, "node_id": "overflow-node"},
            )
        )
        is False
    )
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.EDGES_REGISTERED,
            {"edges": [{"source": "bounded", "target": "edge-one"}]},
        )
    )
    assert (
        actor.ingest(
            _progress_wire(
                WorkflowProgressEventKind.EDGES_REGISTERED,
                {"edges": [{"source": "bounded", "target": "edge-two"}]},
            )
        )
        is False
    )
    for index in range(4):
        assert actor.ingest(
            _progress_wire(
                WorkflowProgressEventKind.SUBMITTED,
                {
                    "node_id": "bounded",
                    "label": f"bounded-{index}",
                    "ray_task_id": f"task-{index}",
                },
            )
        )

    snapshot = actor.snapshot()
    assert snapshot["ingress"]["retained_nodes"] == 1
    assert snapshot["ingress"]["retained_edges"] == 1
    assert snapshot["ingress"]["retained_bytes"] <= limits.combined_max_decoded_bytes
    assert snapshot["ingress"]["rejected"] == 2
    assert snapshot["ingress"]["rejected_by_reason"]["node_limit"] == 1
    assert snapshot["ingress"]["rejected_by_reason"]["edge_limit"] == 1
    assert len(snapshot["recent_events"]) == 2
    assert "overflow-node" not in json.dumps(snapshot)
    assert "edge-two" not in json.dumps(snapshot)


def test_progress_actor_rejects_replacement_over_retained_byte_limit() -> None:
    limits = replace(
        WORKFLOW_PROGRESS_LIMITS_V1,
        combined_max_decoded_bytes=800,
    )
    actor = _progress_actor(limits=limits)
    payload = {
        "node_id": "replace",
        "label": "short",
        "callable_path": "tests.unit.test_remote.workflow_target",
        "runtime_env": {"mode": "inherit"},
        "ray_options": {},
    }
    assert actor.ingest(_progress_wire(WorkflowProgressEventKind.NODE_REGISTERED, payload))
    before = actor.snapshot()
    oversized_label = "never-retain-" + ("x" * 480)

    assert (
        actor.ingest(
            _progress_wire(
                WorkflowProgressEventKind.NODE_REGISTERED,
                {**payload, "label": oversized_label},
            )
        )
        is False
    )

    snapshot = actor.snapshot()
    assert snapshot["revision"] == before["revision"]
    assert snapshot["updated_at"] == before["updated_at"]
    assert snapshot["graph"] == before["graph"]
    assert snapshot["ingress"]["retained_bytes"] == before["ingress"]["retained_bytes"]
    assert snapshot["ingress"]["rejected_by_reason"]["retained_bytes_limit"] == 1
    assert oversized_label not in json.dumps(snapshot)


def test_progress_actor_counts_protocol_truncation_without_raw_retention() -> None:
    actor = _progress_actor()
    raw_error = "sensitive-" + ("x" * 5000)
    wire = _progress_wire(
        WorkflowProgressEventKind.FAILED,
        {
            "node_id": "failed",
            "label": "failed",
            "error": raw_error,
        },
    )
    decoded = decode_workflow_progress_event(wire)
    assert decoded.truncated is True

    assert actor.ingest(wire)

    snapshot = actor.snapshot()
    assert snapshot["ingress"]["truncated"] == 1
    assert raw_error not in json.dumps(snapshot)
    assert snapshot["graph"]["nodes"][0]["error"] != raw_error


def test_progress_actor_ingress_counters_saturate_at_injected_identity_limit() -> None:
    limits = replace(
        WORKFLOW_PROGRESS_LIMITS_V1,
        identity_max_integer=9,
    )
    actor = _progress_actor(limits=limits)
    for index in range(20):
        assert actor.ingest(
            _progress_wire(
                WorkflowProgressEventKind.SUBMITTED,
                {
                    "node_id": "bounded-counter",
                    "label": "bounded-counter",
                    "ray_task_id": f"task-{index}",
                },
            )
        )
    for _index in range(20):
        assert actor.ingest(b'{"invalid":"counter-secret"}') is False
    for index in range(20):
        assert actor.ingest(
            _progress_wire(
                WorkflowProgressEventKind.FAILED,
                {
                    "node_id": f"failed-{index}",
                    "label": f"failed-{index}",
                    "error": "x" * 5000,
                },
            )
        )

    ingress = actor.snapshot()["ingress"]
    assert ingress["accepted"] == limits.identity_max_integer
    assert ingress["rejected"] == limits.identity_max_integer
    assert ingress["truncated"] == limits.identity_max_integer
    assert (
        ingress["accepted_by_kind"][WorkflowProgressEventKind.SUBMITTED.value]
        == limits.identity_max_integer
    )
    assert ingress["rejected_by_reason"]["protocol_error"] == limits.identity_max_integer


def test_progress_actor_recent_events_are_a_fixed_ring() -> None:
    actor = _progress_actor()
    for index in range(40):
        assert actor.ingest(
            _progress_wire(
                WorkflowProgressEventKind.SUBMITTED,
                {
                    "node_id": "ring",
                    "label": f"ring-{index}",
                    "ray_task_id": f"task-{index}",
                },
            )
        )

    snapshot = actor.snapshot()
    assert len(snapshot["recent_events"]) == 32
    assert snapshot["recent_events"][0]["label"] == "ring-8"
    assert snapshot["recent_events"][-1]["label"] == "ring-39"


def test_progress_actor_drains_ingests_after_disable_without_diagnostics() -> None:
    actor = _progress_actor()
    before = actor.snapshot()

    actor.disable()

    assert (
        actor.ingest(
            _progress_wire(
                WorkflowProgressEventKind.FAILED,
                {"node_id": "late", "label": "late", "error": "late-secret"},
            )
        )
        is False
    )
    assert actor.snapshot() == before
