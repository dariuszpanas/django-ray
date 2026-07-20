"""Module-level Ray executors shared by task and workflow submissions."""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from django_ray.redaction import redact_text, result_metadata
from django_ray.runtime.context import (
    WORKFLOW_PROGRESS_SCHEMA_VERSION,
    WORKFLOW_RUN_IDENTITY_SCHEMA_VERSION,
)


def execute_django_task_remote(
    callable_path: str,
    args_json: str,
    kwargs_json: str,
    task_id: int,
    runtime_env_profile: str | None = None,
    runtime_env_hash: str = "",
    input_reference: str | None = None,
    attempt_number: int | None = None,
    execution_generation: int | None = None,
) -> str:
    """Execute one durable django-ray task on a Ray worker."""
    from django_ray.runtime.context import durable_task_execution
    from django_ray.runtime.entrypoint import execute_task

    print(f"[Task {task_id}] Starting: {callable_path}", flush=True)
    with durable_task_execution(
        task_id,
        attempt_number=attempt_number,
        execution_generation=execution_generation,
        runtime_env_profile=runtime_env_profile,
        runtime_env_hash=runtime_env_hash,
    ):
        if input_reference is None:
            result = execute_task(callable_path, args_json, kwargs_json)
        else:
            result = execute_task(
                callable_path,
                args_json,
                kwargs_json,
                input_reference=input_reference,
            )

    parsed = json.loads(result)
    if parsed.get("success"):
        metadata = result_metadata(parsed.get("result"))
        print(f"[Task {task_id}] SUCCESS: {metadata}", flush=True)
    else:
        print(
            f"[Task {task_id}] FAILED: {redact_text(parsed.get('error'))}",
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
    task_execution_pk: int | None,
    progress_actor: Any | None,
    node_id: str,
    *input_args: Any,
    workflow_run_identity: dict[str, Any] | None = None,
) -> Any:
    """Execute a lightweight workflow step without database coordination."""
    if bootstrap_django:
        from django_ray.runtime.entrypoint import bootstrap_django as setup_django

        setup_django()

    from django_ray.logging import configure_default_logging, get_logger
    from django_ray.runtime.context import workflow_step_execution
    from django_ray.runtime.import_utils import import_callable

    callable_obj = import_callable(callable_path)
    kwargs = {**input_kwargs, **bound_kwargs}
    label = callable_path.rsplit(".", 1)[-1]
    execution = _ray_execution_metadata()
    configure_default_logging()
    logger = get_logger(
        "django_ray.workflow",
        component="workflow_step",
        django_task_execution_pk=task_execution_pk,
        workflow_node_id=node_id,
        callable_path=callable_path,
        workflow_run_id=(workflow_run_identity or {}).get("run_id"),
        workflow_attempt_number=(workflow_run_identity or {}).get("attempt_number"),
        workflow_execution_generation=(workflow_run_identity or {}).get("execution_generation"),
        **execution,
    )
    if progress_actor is not None:
        progress_actor.started.remote(node_id, label, execution)
    logger.info("Workflow step started")
    try:
        with workflow_step_execution(
            progress_actor,
            node_id,
            workflow_run_identity,
        ):
            result = callable_obj(*input_args, *bound_args, **kwargs)
    except BaseException as error:
        if progress_actor is not None:
            progress_actor.failed.remote(node_id, label, str(error))
        logger.exception("Workflow step failed")
        raise
    if progress_actor is not None:
        progress_actor.completed.remote(node_id, label)
    logger.info("Workflow step completed")
    return result


def _ray_execution_metadata() -> dict[str, Any]:
    """Return stable Ray identifiers when called from a real Ray worker."""
    try:
        import ray

        context = ray.get_runtime_context()
        return {
            "ray_task_id": str(context.get_task_id()),
            "ray_job_id": str(context.get_job_id()),
            "ray_node_id": str(context.get_node_id()),
            "ray_worker_id": str(context.get_worker_id()),
            "assigned_resources": context.get_assigned_resources(),
        }
    except (AttributeError, RuntimeError, AssertionError):
        return {}


def collect_workflow_results_remote(*values: Any) -> list[Any]:
    """Collect Ray-resolved top-level arguments into an ordered list."""
    return list(values)


class WorkflowProgressActor:
    """In-memory progress collector for one active workflow."""

    def __init__(
        self,
        task_execution_pk: int | None = None,
        attempt_number: int | None = None,
        execution_generation: int | None = None,
        workflow_run_id: str | None = None,
    ) -> None:
        self.started_at = time.time()
        self.updated_at = self.started_at
        self.task_execution_pk = task_execution_pk
        self.run_identity = (
            {
                "schema_version": WORKFLOW_RUN_IDENTITY_SCHEMA_VERSION,
                "run_id": workflow_run_id,
                "task_execution_pk": task_execution_pk,
                "attempt_number": attempt_number,
                "execution_generation": execution_generation,
            }
            if task_execution_pk is not None
            and attempt_number is not None
            and execution_generation is not None
            and workflow_run_id is not None
            else None
        )
        self.accepting_updates = True
        self.revision = 0
        self.nodes: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []

    def _touch(self) -> None:
        self.revision += 1
        self.updated_at = time.time()

    def _event(
        self,
        node_id: str,
        event: str,
        label: str,
        *,
        state: str | None = None,
    ) -> None:
        self._touch()
        self.events.append(
            {
                "node_id": node_id,
                "event": event,
                "state": state or event,
                "label": label,
                "timestamp": self.updated_at,
            }
        )
        self.events = self.events[-50:]

    def register(
        self,
        node_id: str,
        label: str,
        callable_path: str | None = None,
        dependencies: list[str] | None = None,
        runtime_env: dict[str, Any] | None = None,
        ray_options: dict[str, Any] | None = None,
    ) -> None:
        if not self.accepting_updates:
            return
        if node_id in self.nodes:
            node = self.nodes[node_id]
            changed = False
            if label is not None and node["label"] != label:
                node["label"] = label
                changed = True
            if callable_path is not None and node["callable_path"] != callable_path:
                node["callable_path"] = callable_path
                changed = True
            if dependencies is not None and node["dependencies"] != dependencies:
                node["dependencies"] = dependencies
                changed = True
            if runtime_env is not None and node["runtime_env"] != runtime_env:
                node["runtime_env"] = runtime_env
                changed = True
            if ray_options is not None and node["ray_options"] != ray_options:
                node["ray_options"] = ray_options
                changed = True
            if changed:
                self._touch()
            return
        self.nodes[node_id] = {
            "node_id": node_id,
            "kind": "task",
            "label": label,
            "callable_path": callable_path,
            "dependencies": dependencies or [],
            "runtime_env": runtime_env or {"mode": "inherit"},
            "ray_options": ray_options or {},
            "state": "PENDING",
            "progress": None,
            "execution": {},
            "started_at": None,
            "finished_at": None,
            "error": None,
        }
        self._touch()

    def register_map(
        self,
        node_id: str,
        label: str,
        dependencies: list[str],
        max_concurrency: int | None,
        max_items: int | None,
    ) -> None:
        """Register one aggregate node for a bounded dynamic map."""
        if not self.accepting_updates:
            return
        self.register(node_id, label, dependencies=dependencies)
        node = self.nodes[node_id]
        node["kind"] = "map"
        node["state"] = "RUNNING"
        node["started_at"] = time.time()
        node["fanout"] = {
            "max_concurrency": max_concurrency,
            "max_items": max_items,
            "submitted_items": 0,
            "completed_items": 0,
            "in_flight_items": 0,
            "input_exhausted": False,
        }
        self._event(node_id, "STARTED", label, state="RUNNING")

    def map_progress(
        self,
        node_id: str,
        label: str,
        submitted: int,
        completed: int,
        input_exhausted: bool,
    ) -> None:
        """Update aggregate counters without retaining one node per map item."""
        if not self.accepting_updates:
            return
        if node_id not in self.nodes:
            self.register_map(node_id, label, [], None, None)
        node = self.nodes[node_id]
        node["fanout"].update(
            {
                "submitted_items": submitted,
                "completed_items": completed,
                "in_flight_items": submitted - completed,
                "input_exhausted": input_exhausted,
            }
        )
        if input_exhausted:
            percent = 100.0 if submitted == 0 else round(completed / submitted * 100, 1)
            node["progress"] = {
                "current": completed,
                "total": submitted,
                "percent": percent,
                "message": "Collecting bounded map results",
                "metrics": dict(node["fanout"]),
                "updated_at": time.time(),
            }
        self._event(node_id, "PROGRESS", label, state=node["state"])

    def started(
        self,
        node_id: str,
        label: str,
        execution: dict[str, Any] | None = None,
    ) -> None:
        if not self.accepting_updates:
            return
        self.register(node_id, label)
        node = self.nodes[node_id]
        node["state"] = "RUNNING"
        node["started_at"] = time.time()
        node["execution"] = execution or {}
        self._event(node_id, "STARTED", label, state="RUNNING")

    def submitted(self, node_id: str, label: str, ray_task_id: str) -> None:
        if not self.accepting_updates:
            return
        self.register(node_id, label)
        node = self.nodes[node_id]
        node["execution"] = {
            **node["execution"],
            "ray_task_id": ray_task_id,
        }
        self._event(node_id, "SUBMITTED", label, state=node["state"])

    def completed(self, node_id: str, label: str) -> None:
        if not self.accepting_updates:
            return
        self.register(node_id, label)
        node = self.nodes[node_id]
        node["state"] = "SUCCEEDED"
        node["finished_at"] = time.time()
        if node["kind"] == "map":
            submitted = node["fanout"]["submitted_items"]
            node["fanout"].update(
                {
                    "completed_items": submitted,
                    "in_flight_items": 0,
                    "input_exhausted": True,
                }
            )
        if node["progress"] is not None:
            node["progress"]["current"] = node["progress"]["total"]
            node["progress"]["percent"] = 100.0
            if node["kind"] == "map":
                node["progress"]["metrics"] = dict(node["fanout"])
        self._event(node_id, "COMPLETED", label, state="SUCCEEDED")

    def failed(self, node_id: str, label: str, error: str) -> None:
        if not self.accepting_updates:
            return
        self.register(node_id, label)
        node = self.nodes[node_id]
        node["state"] = "FAILED"
        node["finished_at"] = time.time()
        node["error"] = error
        self._event(node_id, "FAILED", label, state="FAILED")

    def progress(
        self,
        node_id: str,
        current: float,
        total: float,
        message: str | None,
        metrics: dict[str, Any],
    ) -> None:
        if not self.accepting_updates:
            return
        if node_id not in self.nodes:
            self.register(node_id, node_id)
        node = self.nodes[node_id]
        node["progress"] = {
            "current": current,
            "total": total,
            "percent": round(current / total * 100, 1),
            "message": message,
            "metrics": metrics,
            "updated_at": time.time(),
        }
        self._event(node_id, "PROGRESS", node["label"], state=node["state"])

    def disable(self) -> None:
        """Drain future leaf reports without mutating this obsolete snapshot."""
        self.accepting_updates = False

    def snapshot(self) -> dict[str, Any]:
        states = [node["state"] for node in self.nodes.values()]
        completed = states.count("SUCCEEDED")
        failed = states.count("FAILED")
        total = len(states)
        terminal = completed + failed
        nodes = list(self.nodes.values())
        edges = [
            {"source": dependency, "target": node["node_id"]}
            for node in nodes
            for dependency in node["dependencies"]
        ]
        return {
            "schema_version": WORKFLOW_PROGRESS_SCHEMA_VERSION,
            "workflow_id": (
                f"django-ray:{self.task_execution_pk}"
                if self.task_execution_pk is not None
                else None
            ),
            "run_identity": self.run_identity,
            "revision": self.revision,
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
            "updated_at": self.updated_at,
            "graph": {
                "nodes": nodes,
                "edges": edges,
            },
            "recent_events": list(self.events),
        }
