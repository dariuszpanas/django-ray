"""Tests for workflow graph and Ray live-observability helpers."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from django_ray.models import RayTaskExecution
from django_ray.observability import (
    WorkflowObservabilityError,
    get_ray_task_logs,
    get_ray_task_state,
    get_workflow_graph,
    get_workflow_node,
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
