"""Tests for workflow graph and Ray live-observability helpers."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
from django.core.exceptions import ImproperlyConfigured

import django_ray.observability as observability_module
from django_ray.models import RayTaskExecution
from django_ray.observability import (
    WorkflowObservabilityError,
    get_ray_task_logs,
    get_ray_task_state,
    get_workflow_graph,
    get_workflow_node,
    get_workflow_progress,
)


@pytest.fixture
def workflow_execution(db) -> RayTaskExecution:
    return RayTaskExecution.objects.create(
        task_id="workflow-observability-1",
        callable_path="testproject.tasks.workflow",
        progress_data=json.dumps(
            {
                "schema_version": 1,
                "revision": 3,
                "graph": {
                    "nodes": [
                        {
                            "node_id": "0.0",
                            "dependencies": [],
                            "execution": {"ray_task_id": "ray-task-1"},
                        },
                        {
                            "node_id": "0.1",
                            "dependencies": ["0.0"],
                            "execution": {},
                        },
                    ],
                    "edges": [{"source": "0.0", "target": "0.1"}],
                },
            }
        ),
    )


def test_get_workflow_graph_and_node(workflow_execution) -> None:
    graph = get_workflow_graph(workflow_execution)
    node = get_workflow_node(workflow_execution, "0.0")

    assert graph is not None
    assert node is not None
    assert graph["edges"] == [{"source": "0.0", "target": "0.1"}]
    assert node["execution"] == {"ray_task_id": "ray-task-1"}
    assert get_workflow_node(workflow_execution, "missing") is None


def test_get_ray_task_state_serializes_attempts(monkeypatch, settings) -> None:
    settings.DJANGO_RAY = {
        "RAY_ADDRESS": "auto",
        "RAY_STATE_API_ADDRESS": "http://ray-dashboard:8265",
    }
    fake_state = SimpleNamespace(
        get_task=lambda **kwargs: [
            SimpleNamespace(asdict=lambda: {"task_id": kwargs["id"], "state": "RUNNING"})
        ],
        get_log=lambda **kwargs: [],
    )
    monkeypatch.setitem(sys.modules, "ray.util.state", fake_state)

    assert get_ray_task_state("ray-task-1") == [{"task_id": "ray-task-1", "state": "RUNNING"}]


def test_get_ray_task_logs_returns_bounded_streams(monkeypatch, settings) -> None:
    settings.DJANGO_RAY = {
        "RAY_ADDRESS": "auto",
        "RAY_STATE_API_ADDRESS": "http://ray-dashboard:8265",
    }
    fake_state = SimpleNamespace(
        get_task=lambda **kwargs: None,
        get_log=lambda **kwargs: iter([f"{kwargs['suffix']}-line\n"]),
    )
    monkeypatch.setitem(sys.modules, "ray.util.state", fake_state)

    assert get_ray_task_logs("ray-task-1", tail=20) == {
        "out": "out-line\n",
        "err": "err-line\n",
    }


def test_get_ray_task_state_wraps_state_api_errors(monkeypatch, settings) -> None:
    settings.DJANGO_RAY = {
        "RAY_ADDRESS": "auto",
        "RAY_STATE_API_ADDRESS": "http://ray-dashboard:8265",
    }
    fake_state = SimpleNamespace(
        get_task=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
        get_log=lambda **kwargs: [],
    )
    monkeypatch.setitem(sys.modules, "ray.util.state", fake_state)

    with pytest.raises(WorkflowObservabilityError, match="offline"):
        get_ray_task_state("ray-task-1")


@pytest.mark.parametrize("progress_data", [None, ""])
def test_get_workflow_progress_returns_none_without_snapshot(progress_data) -> None:
    execution = SimpleNamespace(progress_data=progress_data, task_id="task-1")

    assert get_workflow_progress(execution) is None
    assert get_workflow_graph(execution) is None
    assert get_workflow_node(execution, "0.0") is None


@pytest.mark.parametrize("progress_data", ["{", "[]"])
def test_get_workflow_progress_rejects_invalid_snapshots(progress_data) -> None:
    execution = SimpleNamespace(progress_data=progress_data, task_id="task-1")

    with pytest.raises(WorkflowObservabilityError):
        get_workflow_progress(execution)


def test_get_workflow_graph_builds_legacy_edges() -> None:
    execution = SimpleNamespace(
        task_id="task-1",
        progress_data=json.dumps(
            {
                "nodes": [
                    {"node_id": "0.0", "dependencies": []},
                    {"node_id": "0.1", "dependencies": ["0.0"]},
                    "ignored",
                ]
            }
        ),
    )

    assert get_workflow_graph(execution) == {
        "nodes": [
            {"node_id": "0.0", "dependencies": []},
            {"node_id": "0.1", "dependencies": ["0.0"]},
            "ignored",
        ],
        "edges": [{"source": "0.0", "target": "0.1"}],
    }

    execution.progress_data = json.dumps({"nodes": "not-a-list"})
    assert get_workflow_graph(execution) == {"nodes": [], "edges": []}


def test_get_ray_task_state_handles_none_and_object_shapes(monkeypatch, settings) -> None:
    settings.DJANGO_RAY = {"RAY_STATE_API_ADDRESS": "http://ray-dashboard:8265"}
    result = None

    def _get_task(**kwargs):
        return result

    fake_state = SimpleNamespace(get_task=_get_task, get_log=lambda **kwargs: [])
    monkeypatch.setitem(sys.modules, "ray.util.state", fake_state)

    assert get_ray_task_state("ray-task-1") == []

    result = [
        {"state": "RUNNING"},
        SimpleNamespace(state="FAILED"),
        object(),
    ]
    attempts = get_ray_task_state("ray-task-1")

    assert attempts[0] == {"state": "RUNNING"}
    assert attempts[1] == {"state": "FAILED"}
    assert attempts[2]["raw"].startswith("<object object at ")


@pytest.mark.parametrize("tail", [0, 1001])
def test_get_ray_task_logs_validates_tail(tail) -> None:
    with pytest.raises(ValueError, match="between 1 and 1000"):
        get_ray_task_logs("ray-task-1", tail=tail)


def test_get_ray_task_logs_wraps_state_api_errors(monkeypatch, settings) -> None:
    settings.DJANGO_RAY = {"RAY_STATE_API_ADDRESS": "http://ray-dashboard:8265"}
    fake_state = SimpleNamespace(
        get_task=lambda **kwargs: None,
        get_log=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("logs offline")),
    )
    monkeypatch.setitem(sys.modules, "ray.util.state", fake_state)

    with pytest.raises(WorkflowObservabilityError, match="logs offline"):
        get_ray_task_logs("ray-task-1")


def test_state_api_address_uses_explicit_or_initialized_ray(monkeypatch, settings) -> None:
    settings.DJANGO_RAY = {}

    assert observability_module._state_api_address("http://explicit:8265") == (
        "http://explicit:8265"
    )

    monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: True))
    assert observability_module._state_api_address(None) is None


def test_state_api_address_is_required_without_ray(monkeypatch, settings) -> None:
    settings.DJANGO_RAY = {}
    monkeypatch.setitem(sys.modules, "ray", None)

    with pytest.raises(ImproperlyConfigured, match="RAY_STATE_API_ADDRESS is required"):
        observability_module._state_api_address(None)
