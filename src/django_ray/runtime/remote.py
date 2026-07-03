"""Module-level Ray executors shared by task and workflow submissions."""

from __future__ import annotations

import json
import sys
import time
from typing import Any


def execute_django_task_remote(
    callable_path: str,
    args_json: str,
    kwargs_json: str,
    task_id: int,
) -> str:
    """Execute one durable django-ray task on a Ray worker."""
    from django_ray.runtime.context import durable_task_execution
    from django_ray.runtime.entrypoint import execute_task

    print(f"[Task {task_id}] Starting: {callable_path}", flush=True)
    with durable_task_execution(task_id):
        result = execute_task(callable_path, args_json, kwargs_json)

    parsed = json.loads(result)
    if parsed.get("success"):
        print(f"[Task {task_id}] SUCCESS: {parsed.get('result')}", flush=True)
    else:
        print(
            f"[Task {task_id}] FAILED: {parsed.get('error')}",
            file=sys.stderr,
            flush=True,
        )

    return result


def execute_workflow_step_remote(
    callable_path: str,
    bootstrap_django: bool,
    bound_args: tuple[Any, ...],
    bound_kwargs: dict[str, Any],
    input_kwargs: dict[str, Any],
    progress_actor: Any | None,
    node_id: str,
    *input_args: Any,
) -> Any:
    """Execute a lightweight workflow step without database coordination."""
    if bootstrap_django:
        from django_ray.runtime.entrypoint import bootstrap_django as setup_django

        setup_django()

    from django_ray.runtime.import_utils import import_callable

    callable_obj = import_callable(callable_path)
    kwargs = {**input_kwargs, **bound_kwargs}
    label = callable_path.rsplit(".", 1)[-1]
    if progress_actor is not None:
        progress_actor.started.remote(node_id, label)
    try:
        result = callable_obj(*input_args, *bound_args, **kwargs)
    except BaseException as error:
        if progress_actor is not None:
            progress_actor.failed.remote(node_id, label, str(error))
        raise
    if progress_actor is not None:
        progress_actor.completed.remote(node_id, label)
    return result


def collect_workflow_results_remote(*values: Any) -> list[Any]:
    """Collect Ray-resolved top-level arguments into an ordered list."""
    return list(values)


class WorkflowProgressActor:
    """In-memory progress collector for one active workflow."""

    def __init__(self) -> None:
        self.started_at = time.time()
        self.nodes: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []

    def _event(self, node_id: str, state: str, label: str) -> None:
        self.events.append(
            {
                "node_id": node_id,
                "state": state,
                "label": label,
                "timestamp": time.time(),
            }
        )
        self.events = self.events[-50:]

    def register(self, node_id: str, label: str) -> None:
        self.nodes.setdefault(
            node_id,
            {
                "node_id": node_id,
                "label": label,
                "state": "PENDING",
                "started_at": None,
                "finished_at": None,
                "error": None,
            },
        )

    def started(self, node_id: str, label: str) -> None:
        self.register(node_id, label)
        node = self.nodes[node_id]
        node["state"] = "RUNNING"
        node["started_at"] = time.time()
        self._event(node_id, "RUNNING", label)

    def completed(self, node_id: str, label: str) -> None:
        self.register(node_id, label)
        node = self.nodes[node_id]
        node["state"] = "SUCCEEDED"
        node["finished_at"] = time.time()
        self._event(node_id, "SUCCEEDED", label)

    def failed(self, node_id: str, label: str, error: str) -> None:
        self.register(node_id, label)
        node = self.nodes[node_id]
        node["state"] = "FAILED"
        node["finished_at"] = time.time()
        node["error"] = error
        self._event(node_id, "FAILED", label)

    def snapshot(self) -> dict[str, Any]:
        states = [node["state"] for node in self.nodes.values()]
        completed = states.count("SUCCEEDED")
        failed = states.count("FAILED")
        total = len(states)
        terminal = completed + failed
        return {
            "state": "FAILED"
            if failed
            else ("SUCCEEDED" if total and terminal == total else "RUNNING"),
            "total_nodes": total,
            "completed_nodes": completed,
            "failed_nodes": failed,
            "running_nodes": states.count("RUNNING"),
            "pending_nodes": states.count("PENDING"),
            "progress_percent": round(terminal / total * 100, 1) if total else 0.0,
            "started_at": self.started_at,
            "updated_at": time.time(),
            "nodes": list(self.nodes.values()),
            "recent_events": list(self.events),
        }
