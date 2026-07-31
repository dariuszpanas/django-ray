"""Module-level Ray executors shared by task and workflow submissions."""

from __future__ import annotations

import copy
import json
import sys
import time
from datetime import UTC, datetime
from typing import Any, cast

from django_ray.runtime.context import (
    WORKFLOW_PROGRESS_SCHEMA_VERSION,
)
from django_ray.workflow_progress_limits import (
    canonical_workflow_progress_retained_size,
    workflow_progress_retained_state_size,
)
from django_ray.workflow_progress_protocol import (
    WORKFLOW_PROGRESS_LIMITS_V1,
    WorkflowProgressEvent,
    WorkflowProgressEventKind,
    WorkflowProgressLimits,
    WorkflowProgressProtocolError,
    decode_workflow_progress_event,
    send_workflow_progress_event,
)

_COST_SCHEMA_VERSION = 1
_PRODUCER_SCHEMA_VERSION = 1
_PRODUCER_COUNTER_FIELDS = (
    "offered",
    "submitted",
    "superseded",
    "locally_dropped",
    "acknowledged",
    "actor_rejected",
    "ack_failed",
    "pending_acknowledgements",
)
_PRODUCER_TERMINAL_HANDOFFS = (
    "not_needed",
    "submitted",
    "failed",
    "actor_unavailable",
)


def _wall_time_ns() -> int:
    return time.perf_counter_ns()


def _process_cpu_ns() -> int:
    return time.process_time_ns()


def _utc_time_us() -> int:
    return time.time_ns() // 1_000


def _event_timestamp_us(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    delta = parsed - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 24 * 60 * 60 + delta.seconds) * 1_000_000 + delta.microseconds


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
    runtime_env_plan_identity: dict[str, Any] | None = None,
    compiled_graph_submission_transport: str | None = None,
) -> str:
    """Execute one durable django-ray task on a Ray worker."""
    from django_ray.redaction import redact_text, result_metadata
    from django_ray.runtime.context import durable_task_execution
    from django_ray.runtime.entrypoint import execute_task

    print(f"[Task {task_id}] Starting: {callable_path}", flush=True)
    with durable_task_execution(
        task_id,
        attempt_number=attempt_number,
        execution_generation=execution_generation,
        runtime_env_profile=runtime_env_profile,
        runtime_env_hash=runtime_env_hash,
        runtime_env_plan_identity=runtime_env_plan_identity,
        compiled_graph_submission_transport=compiled_graph_submission_transport,
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
    workflow_progress_limits: WorkflowProgressLimits = WORKFLOW_PROGRESS_LIMITS_V1,
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
    _send_step_progress_event(
        progress_actor,
        workflow_run_identity,
        WorkflowProgressEventKind.STARTED,
        {
            "node_id": node_id,
            "label": label,
            "execution": execution,
        },
        limits=workflow_progress_limits,
    )
    logger.info("Workflow step started")
    try:
        with workflow_step_execution(
            progress_actor,
            node_id,
            workflow_run_identity,
            workflow_progress_limits,
        ):
            result = callable_obj(*input_args, *bound_args, **kwargs)
    except BaseException as error:
        try:
            failure_payload = {
                "node_id": node_id,
                "label": label,
                "error": str(error),
            }
        except Exception:
            failure_payload = {
                "node_id": node_id,
                "label": label,
                "error": "Workflow step failed",
            }
        _send_step_progress_event(
            progress_actor,
            workflow_run_identity,
            WorkflowProgressEventKind.FAILED,
            failure_payload,
            limits=workflow_progress_limits,
        )
        logger.exception("Workflow step failed")
        raise
    _send_step_progress_event(
        progress_actor,
        workflow_run_identity,
        WorkflowProgressEventKind.COMPLETED,
        {
            "node_id": node_id,
            "label": label,
        },
        limits=workflow_progress_limits,
    )
    logger.info("Workflow step completed")
    return result


def _send_step_progress_event(
    progress_actor: Any | None,
    workflow_run_identity: dict[str, Any] | None,
    kind: WorkflowProgressEventKind,
    payload: dict[str, Any],
    *,
    limits: WorkflowProgressLimits,
) -> None:
    """Report one leaf event without making observability task-critical."""
    if progress_actor is None or workflow_run_identity is None:
        return
    try:
        send_workflow_progress_event(
            progress_actor,
            workflow_run_identity,
            kind,
            payload,
            limits=limits,
        )
    except Exception:
        return


def _ray_execution_metadata() -> dict[str, Any]:
    """Return identifiers from an already-running Ray process without importing Ray."""
    ray: Any = sys.modules.get("ray")
    if ray is None:
        return {}
    try:
        if not ray.is_initialized():
            return {}
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


class _WorkflowProgressCollector:
    """Private bounded state machine behind the three-method Ray actor."""

    def __init__(
        self,
        initialization_event: bytes,
        *,
        limits: WorkflowProgressLimits = WORKFLOW_PROGRESS_LIMITS_V1,
    ) -> None:
        handler_wall_started = _wall_time_ns()
        handler_cpu_started = _process_cpu_ns()
        event = decode_workflow_progress_event(initialization_event, limits=limits)
        if event.kind is not WorkflowProgressEventKind.INITIALIZED:
            raise WorkflowProgressProtocolError(
                "workflow progress actor requires an initialized event"
            )

        self.started_at = time.time()
        self.updated_at = self.started_at
        self._limits = limits
        self._cost_counter_max = limits.identity_max_integer
        self.task_execution_pk = int(event.run_identity["task_execution_pk"])
        self.run_identity = copy.deepcopy(event.run_identity)
        self.accepting_updates = True
        self.plan_summary = copy.deepcopy(event.payload["plan"])
        self.revision = 0
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: set[tuple[str, str]] = set()
        self.events: list[dict[str, Any]] = []
        self._node_sizes: dict[str, int] = {}
        self._edge_sizes: dict[tuple[str, str], int] = {}
        self._event_sizes: list[int] = []
        self._node_payload_bytes = 0
        self._edge_payload_bytes = 0
        self._event_payload_bytes = 0
        self._plan_size = canonical_workflow_progress_retained_size(self.plan_summary)
        self._retained_bytes = workflow_progress_retained_state_size(
            plan_bytes=self._plan_size,
            node_bytes=0,
            node_count=0,
            edge_bytes=0,
            edge_count=0,
            event_bytes=0,
            event_count=0,
        )
        if self._retained_bytes > self._retained_bytes_limit:
            raise WorkflowProgressProtocolError(
                "workflow progress initialization exceeds the retained-byte limit"
            )

        self._accepted = 1
        self._rejected = 0
        self._truncated = int(event.truncated)
        self._accepted_by_kind = {kind.value: 0 for kind in WorkflowProgressEventKind}
        self._accepted_by_kind[WorkflowProgressEventKind.INITIALIZED.value] = 1
        self._rejected_by_reason = {
            "protocol_error": 0,
            "fence_mismatch": 0,
            "unexpected_initialized": 0,
            "node_limit": 0,
            "edge_limit": 0,
            "retained_bytes_limit": 0,
        }
        self._cost = self._initial_cost(
            initialization_event,
            handler_wall_ns=max(0, _wall_time_ns() - handler_wall_started),
            handler_cpu_ns=max(0, _process_cpu_ns() - handler_cpu_started),
        )
        self._producer: dict[str, Any] = {
            "schema_version": _PRODUCER_SCHEMA_VERSION,
            "saturated": False,
            "reports": 0,
            **dict.fromkeys(_PRODUCER_COUNTER_FIELDS, 0),
            "terminal_handoffs": dict.fromkeys(
                _PRODUCER_TERMINAL_HANDOFFS,
                0,
            ),
        }

    @staticmethod
    def _empty_kind_counters() -> dict[str, int]:
        return {kind.value: 0 for kind in WorkflowProgressEventKind}

    def _initial_cost(
        self,
        initialization_event: bytes,
        *,
        handler_wall_ns: int,
        handler_cpu_ns: int,
    ) -> dict[str, Any]:
        cost: dict[str, Any] = {
            "schema_version": _COST_SCHEMA_VERSION,
            "saturated": False,
            "initialization": {
                "wire_bytes": 0,
                "handler_wall_ns": 0,
                "handler_cpu_ns": 0,
            },
            "ingest": {
                "calls_received": 0,
                "wire_bytes_received": 0,
                "decoded_calls": 0,
                "post_disable_calls": 0,
                "decoded_by_kind": self._empty_kind_counters(),
                "handler_wall_ns_total": 0,
                "handler_wall_ns_max": 0,
                "handler_cpu_ns_total": 0,
                "handler_cpu_ns_max": 0,
            },
            "delivery_delay": {
                "samples": 0,
                "total_us": 0,
                "max_us": 0,
                "negative_clock_samples": 0,
            },
            "snapshot": {
                "calls": 0,
                "build_wall_ns_total": 0,
                "build_wall_ns_max": 0,
                "build_cpu_ns_total": 0,
                "build_cpu_ns_max": 0,
            },
        }
        self._cost = cost
        self._set_cost_counter(
            cost["initialization"],
            "wire_bytes",
            len(initialization_event),
        )
        self._set_cost_counter(
            cost["initialization"],
            "handler_wall_ns",
            handler_wall_ns,
        )
        self._set_cost_counter(
            cost["initialization"],
            "handler_cpu_ns",
            handler_cpu_ns,
        )
        return cost

    def _bounded_cost_value(self, value: int) -> int:
        if value >= self._cost_counter_max:
            if value > self._cost_counter_max:
                self._cost["saturated"] = True
            return self._cost_counter_max
        return value

    def _set_cost_counter(
        self,
        section: dict[str, Any],
        field: str,
        value: int,
    ) -> None:
        section[field] = self._bounded_cost_value(max(0, value))

    def _add_cost_counter(
        self,
        section: dict[str, Any],
        field: str,
        increment: int,
    ) -> None:
        current = int(section[field])
        increment = max(0, increment)
        if increment == 0:
            return
        if current >= self._cost_counter_max or increment > self._cost_counter_max - current:
            section[field] = self._cost_counter_max
            self._cost["saturated"] = True
            return
        section[field] = current + increment

    def _observe_cost_max(
        self,
        section: dict[str, Any],
        field: str,
        value: int,
    ) -> None:
        bounded = self._bounded_cost_value(max(0, value))
        section[field] = max(int(section[field]), bounded)

    def _record_ingest_handler_cost(
        self,
        *,
        wall_started: int,
        cpu_started: int,
    ) -> None:
        wall_ns = max(0, _wall_time_ns() - wall_started)
        cpu_ns = max(0, _process_cpu_ns() - cpu_started)
        ingest = self._cost["ingest"]
        self._add_cost_counter(ingest, "handler_wall_ns_total", wall_ns)
        self._observe_cost_max(ingest, "handler_wall_ns_max", wall_ns)
        self._add_cost_counter(ingest, "handler_cpu_ns_total", cpu_ns)
        self._observe_cost_max(ingest, "handler_cpu_ns_max", cpu_ns)

    def _record_delivery_delay(self, event: WorkflowProgressEvent, received_at_us: int) -> None:
        delay_us = received_at_us - _event_timestamp_us(event.occurred_at)
        delivery = self._cost["delivery_delay"]
        if delay_us < 0:
            self._add_cost_counter(delivery, "negative_clock_samples", 1)
            return
        self._add_cost_counter(delivery, "samples", 1)
        self._add_cost_counter(delivery, "total_us", delay_us)
        self._observe_cost_max(delivery, "max_us", delay_us)

    def _add_producer_counter(
        self,
        section: dict[str, Any],
        field: str,
        increment: int,
    ) -> None:
        current = int(section[field])
        if increment == 0:
            return
        if current >= self._counter_max or increment > self._counter_max - current:
            section[field] = self._counter_max
            self._producer["saturated"] = True
            return
        section[field] = current + increment

    def _aggregate_producer_report(self, event: WorkflowProgressEvent) -> None:
        report = event.payload
        if report["saturated"]:
            self._producer["saturated"] = True
        self._add_producer_counter(
            self._producer,
            "reports",
            1,
        )
        for field in _PRODUCER_COUNTER_FIELDS:
            self._add_producer_counter(
                self._producer,
                field,
                int(report[field]),
            )
        terminal_handoff = cast(
            dict[str, Any],
            self._producer["terminal_handoffs"],
        )
        self._add_producer_counter(
            terminal_handoff,
            str(report["terminal_handoff"]),
            1,
        )

    @property
    def _node_limit(self) -> int:
        return self._limits.topology_node_max_items

    @property
    def _edge_limit(self) -> int:
        return self._limits.topology_edge_max_items

    @property
    def _retained_bytes_limit(self) -> int:
        return self._limits.combined_max_decoded_bytes

    @property
    def _recent_event_limit(self) -> int:
        return self._limits.recent_event_max_items

    @property
    def _counter_max(self) -> int:
        return self._limits.identity_max_integer

    def _touch(self) -> None:
        self.revision = min(self._counter_max, self.revision + 1)
        self.updated_at = time.time()

    def _increment_counter(self, attribute: str) -> None:
        value = int(getattr(self, attribute))
        setattr(self, attribute, min(self._counter_max, value + 1))

    def _reject(self, reason: str) -> bool:
        self._increment_counter("_rejected")
        self._rejected_by_reason[reason] = min(
            self._counter_max,
            self._rejected_by_reason[reason] + 1,
        )
        return False

    def _accept(self, event: WorkflowProgressEvent) -> bool:
        self._increment_counter("_accepted")
        kind = event.kind.value
        self._accepted_by_kind[kind] = min(
            self._counter_max,
            self._accepted_by_kind[kind] + 1,
        )
        if event.truncated:
            self._increment_counter("_truncated")
        self._touch()
        return True

    def _placeholder(self, node_id: str, label: str | None = None) -> dict[str, Any]:
        return {
            "node_id": node_id,
            "kind": "task",
            "label": label or node_id,
            "callable_path": None,
            "dependencies": [],
            "runtime_env": {"mode": "inherit"},
            "ray_options": {},
            "state": "PENDING",
            "progress": None,
            "execution": {},
            "started_at": None,
            "finished_at": None,
            "error": None,
        }

    def _candidate_node(self, node_id: str, label: str | None = None) -> dict[str, Any]:
        node = self.nodes.get(node_id)
        return copy.deepcopy(node) if node is not None else self._placeholder(node_id, label)

    def _recent_event(
        self,
        node: dict[str, Any],
        event: str,
        occurred_at: float,
    ) -> dict[str, Any]:
        return {
            "node_id": node["node_id"],
            "event": event,
            "state": node["state"],
            "label": node["label"],
            "timestamp": occurred_at,
        }

    def _commit(
        self,
        *,
        node_updates: dict[str, dict[str, Any]] | None = None,
        edge_additions: set[tuple[str, str]] | None = None,
        recent_event: dict[str, Any] | None = None,
    ) -> str | None:
        node_updates = {} if node_updates is None else node_updates
        edge_additions = set() if edge_additions is None else edge_additions
        new_node_count = sum(node_id not in self.nodes for node_id in node_updates)
        if len(self.nodes) + new_node_count > self._node_limit:
            return "node_limit"
        new_edges = edge_additions - self.edges
        if len(self.edges) + len(new_edges) > self._edge_limit:
            return "edge_limit"

        node_sizes = {
            node_id: canonical_workflow_progress_retained_size(node)
            for node_id, node in node_updates.items()
        }
        edge_sizes = {
            edge: canonical_workflow_progress_retained_size({"source": edge[0], "target": edge[1]})
            for edge in new_edges
        }
        candidate_events = list(self.events)
        candidate_event_sizes = list(self._event_sizes)
        event_payload_bytes = self._event_payload_bytes
        if recent_event is not None and self._recent_event_limit:
            candidate_events.append(recent_event)
            recent_event_size = canonical_workflow_progress_retained_size(recent_event)
            candidate_event_sizes.append(recent_event_size)
            event_payload_bytes += recent_event_size
            excess = len(candidate_events) - self._recent_event_limit
            if excess > 0:
                event_payload_bytes -= sum(candidate_event_sizes[:excess])
                del candidate_events[:excess]
                del candidate_event_sizes[:excess]

        node_payload_bytes = self._node_payload_bytes
        for node_id, size in node_sizes.items():
            node_payload_bytes += size - self._node_sizes.get(node_id, 0)
        edge_payload_bytes = self._edge_payload_bytes + sum(edge_sizes.values())
        retained_bytes = workflow_progress_retained_state_size(
            plan_bytes=self._plan_size,
            node_bytes=node_payload_bytes,
            node_count=len(self.nodes) + new_node_count,
            edge_bytes=edge_payload_bytes,
            edge_count=len(self.edges) + len(new_edges),
            event_bytes=event_payload_bytes,
            event_count=len(candidate_events),
        )
        if retained_bytes > self._retained_bytes_limit:
            return "retained_bytes_limit"

        for node_id, node in node_updates.items():
            self.nodes[node_id] = node
            self._node_sizes[node_id] = node_sizes[node_id]
        self.edges.update(new_edges)
        self._edge_sizes.update(edge_sizes)
        self.events = candidate_events
        self._event_sizes = candidate_event_sizes
        self._node_payload_bytes = node_payload_bytes
        self._edge_payload_bytes = edge_payload_bytes
        self._event_payload_bytes = event_payload_bytes
        self._retained_bytes = retained_bytes
        return None

    def _node_event_candidate(
        self,
        event: WorkflowProgressEvent,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
        payload = event.payload
        node_id = payload["node_id"]
        label = payload.get("label")
        node = self._candidate_node(node_id, label)
        occurred_at = _event_timestamp(event.occurred_at)
        terminal = node["state"] in _TERMINAL_NODE_STATES
        recent_event: dict[str, Any] | None = None

        if event.kind is WorkflowProgressEventKind.NODE_REGISTERED:
            node["label"] = label
            node["callable_path"] = payload["callable_path"]
            node["runtime_env"] = copy.deepcopy(payload["runtime_env"])
            node["ray_options"] = copy.deepcopy(payload["ray_options"])
        elif event.kind is WorkflowProgressEventKind.MAP_REGISTERED:
            node["kind"] = "map"
            node["label"] = label
            fanout = node.get("fanout")
            if not isinstance(fanout, dict):
                fanout = {
                    "submitted_items": 0,
                    "completed_items": 0,
                    "in_flight_items": 0,
                    "input_exhausted": False,
                }
            fanout.update(
                {
                    "max_concurrency": payload["max_concurrency"],
                    "max_items": payload["max_items"],
                }
            )
            node["fanout"] = fanout
            if not terminal:
                node["state"] = "RUNNING"
                node["started_at"] = node["started_at"] or occurred_at
                recent_event = self._recent_event(node, "STARTED", occurred_at)
        elif event.kind is WorkflowProgressEventKind.SUBMITTED:
            node["label"] = label
            node["execution"] = {
                **node["execution"],
                "ray_task_id": payload["ray_task_id"],
            }
            if not terminal:
                recent_event = self._recent_event(node, "SUBMITTED", occurred_at)
        elif event.kind is WorkflowProgressEventKind.STARTED:
            node["label"] = label
            node["execution"] = {
                **node["execution"],
                **copy.deepcopy(payload["execution"]),
            }
            node["started_at"] = node["started_at"] or occurred_at
            if not terminal:
                node["state"] = "RUNNING"
                recent_event = self._recent_event(node, "STARTED", occurred_at)
        elif event.kind is WorkflowProgressEventKind.APPLICATION_PROGRESS:
            if not terminal:
                current = payload["current"]
                total = payload["total"]
                node["progress"] = {
                    "current": current,
                    "total": total,
                    "percent": round(current / total * 100, 1),
                    "message": payload["message"],
                    "metrics": copy.deepcopy(payload["metrics"]),
                    "updated_at": occurred_at,
                }
                recent_event = self._recent_event(node, "PROGRESS", occurred_at)
        elif event.kind is WorkflowProgressEventKind.MAP_PROGRESS:
            node["kind"] = "map"
            node["label"] = label
            fanout = node.get("fanout")
            if not isinstance(fanout, dict):
                fanout = {
                    "max_concurrency": None,
                    "max_items": None,
                    "submitted_items": 0,
                    "completed_items": 0,
                    "in_flight_items": 0,
                    "input_exhausted": False,
                }
            if not terminal:
                submitted = payload["submitted"]
                completed = payload["completed"]
                input_exhausted = payload["input_exhausted"]
                if node["state"] == "PENDING":
                    node["state"] = "RUNNING"
                    node["started_at"] = node["started_at"] or occurred_at
                fanout.update(
                    {
                        "submitted_items": submitted,
                        "completed_items": completed,
                        "in_flight_items": submitted - completed,
                        "input_exhausted": input_exhausted,
                    }
                )
                node["fanout"] = fanout
                if input_exhausted:
                    percent = 100.0 if submitted == 0 else round(completed / submitted * 100, 1)
                    node["progress"] = {
                        "current": completed,
                        "total": submitted,
                        "percent": percent,
                        "message": "Collecting bounded map results",
                        "metrics": copy.deepcopy(fanout),
                        "updated_at": occurred_at,
                    }
                recent_event = self._recent_event(node, "PROGRESS", occurred_at)
            else:
                node["fanout"] = fanout
        elif event.kind is WorkflowProgressEventKind.COMPLETED:
            node["label"] = label
            if not terminal:
                node["state"] = "SUCCEEDED"
                node["finished_at"] = occurred_at
                if node["kind"] == "map":
                    fanout = node["fanout"]
                    submitted = fanout["submitted_items"]
                    fanout.update(
                        {
                            "completed_items": submitted,
                            "in_flight_items": 0,
                            "input_exhausted": True,
                        }
                    )
                if node["progress"] is not None:
                    node["progress"]["current"] = node["progress"]["total"]
                    node["progress"]["percent"] = 100.0
                    node["progress"]["updated_at"] = occurred_at
                    if node["kind"] == "map":
                        node["progress"]["metrics"] = copy.deepcopy(node["fanout"])
                recent_event = self._recent_event(node, "COMPLETED", occurred_at)
        elif event.kind is WorkflowProgressEventKind.FAILED:
            node["label"] = label
            if not terminal:
                node["state"] = "FAILED"
                node["finished_at"] = occurred_at
                node["error"] = payload["error"]
                recent_event = self._recent_event(node, "FAILED", occurred_at)
        return {node_id: node}, recent_event

    def ingest(self, wire: bytes) -> bool:
        """Decode, fence, and atomically retain one bounded event."""
        handler_wall_started = _wall_time_ns()
        handler_cpu_started = _process_cpu_ns()
        received_at_us = _utc_time_us()
        ingest_cost = cast(dict[str, Any], self._cost["ingest"])
        self._add_cost_counter(ingest_cost, "calls_received", 1)
        if type(wire) is bytes:
            self._add_cost_counter(ingest_cost, "wire_bytes_received", len(wire))
        try:
            if not self.accepting_updates:
                self._add_cost_counter(ingest_cost, "post_disable_calls", 1)
                return False
            try:
                # Preserve the existing fence-before-payload contract. Rejected
                # wrong-run calls remain visible in received bytes/calls and
                # handler cost, but are not successful decoded-kind or delay
                # samples for this run.
                event = decode_workflow_progress_event(
                    wire,
                    expected_run_identity=self.run_identity,
                    limits=self._limits,
                )
            except WorkflowProgressProtocolError as error:
                reason = (
                    "fence_mismatch"
                    if getattr(error, "reason", None) == "fence_mismatch"
                    else "protocol_error"
                )
                return self._reject(reason)

            self._add_cost_counter(ingest_cost, "decoded_calls", 1)
            self._add_cost_counter(
                ingest_cost["decoded_by_kind"],
                event.kind.value,
                1,
            )
            self._record_delivery_delay(event, received_at_us)
            if event.kind is WorkflowProgressEventKind.INITIALIZED:
                return self._reject("unexpected_initialized")
            if event.kind is WorkflowProgressEventKind.PRODUCER_REPORT:
                self._aggregate_producer_report(event)
                return self._accept(event)

            node_updates: dict[str, dict[str, Any]] = {}
            edge_additions: set[tuple[str, str]] = set()
            recent_event = None
            if event.kind is WorkflowProgressEventKind.EDGES_REGISTERED:
                for edge in event.payload["edges"]:
                    source = edge["source"]
                    target = edge["target"]
                    edge_additions.add((source, target))
            else:
                node_updates, recent_event = self._node_event_candidate(event)

            rejection = self._commit(
                node_updates=node_updates,
                edge_additions=edge_additions,
                recent_event=recent_event,
            )
            if rejection is not None:
                return self._reject(rejection)
            return self._accept(event)
        finally:
            self._record_ingest_handler_cost(
                wall_started=handler_wall_started,
                cpu_started=handler_cpu_started,
            )

    def disable(self) -> None:
        """Drain future leaf reports without mutating this obsolete snapshot."""
        self.accepting_updates = False

    def snapshot(self) -> dict[str, Any]:
        build_wall_started = _wall_time_ns()
        build_cpu_started = _process_cpu_ns()
        snapshot_cost = cast(dict[str, Any], self._cost["snapshot"])
        self._add_cost_counter(snapshot_cost, "calls", 1)
        states = [node["state"] for node in self.nodes.values()]
        completed = states.count("SUCCEEDED")
        failed = states.count("FAILED")
        total = len(states)
        terminal = completed + failed
        dependencies: dict[str, list[str]] = {node_id: [] for node_id in self.nodes}
        for source, target in self.edges:
            dependencies.setdefault(target, []).append(source)
        nodes = []
        for node_id in sorted(self.nodes):
            node = copy.deepcopy(self.nodes[node_id])
            node["dependencies"] = sorted(dependencies.get(node_id, []))
            nodes.append(node)
        edges = [{"source": source, "target": target} for source, target in sorted(self.edges)]
        cost = copy.deepcopy(self._cost)
        snapshot = {
            "schema_version": WORKFLOW_PROGRESS_SCHEMA_VERSION,
            "workflow_id": f"django-ray:{self.task_execution_pk}",
            "run_identity": copy.deepcopy(self.run_identity),
            "plan": copy.deepcopy(self.plan_summary),
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
            "recent_events": copy.deepcopy(self.events),
            "ingress": {
                "accepted": self._accepted,
                "rejected": self._rejected,
                "truncated": self._truncated,
                "accepted_by_kind": dict(self._accepted_by_kind),
                "rejected_by_reason": dict(self._rejected_by_reason),
                "retained_bytes": self._retained_bytes,
                "retained_nodes": len(self.nodes),
                "retained_edges": len(self.edges),
                "cost": cost,
                "producer": copy.deepcopy(self._producer),
            },
        }
        build_wall_ns = max(0, _wall_time_ns() - build_wall_started)
        build_cpu_ns = max(0, _process_cpu_ns() - build_cpu_started)
        self._add_cost_counter(snapshot_cost, "build_wall_ns_total", build_wall_ns)
        self._observe_cost_max(snapshot_cost, "build_wall_ns_max", build_wall_ns)
        self._add_cost_counter(snapshot_cost, "build_cpu_ns_total", build_cpu_ns)
        self._observe_cost_max(snapshot_cost, "build_cpu_ns_max", build_cpu_ns)
        cost["saturated"] = self._cost["saturated"]
        cost_snapshot = cast(dict[str, Any], cost["snapshot"])
        for field in (
            "calls",
            "build_wall_ns_total",
            "build_wall_ns_max",
            "build_cpu_ns_total",
            "build_cpu_ns_max",
        ):
            cost_snapshot[field] = snapshot_cost[field]
        return snapshot


class WorkflowProgressActor:
    """Ingest-only Ray surface for one bounded, fenced progress collector."""

    def __init__(
        self,
        initialization_event: bytes,
        *,
        limits: WorkflowProgressLimits = WORKFLOW_PROGRESS_LIMITS_V1,
    ) -> None:
        self.__collector = _WorkflowProgressCollector(
            initialization_event,
            limits=limits,
        )

    def ingest(self, wire: bytes) -> bool:
        """Ingest one canonical event through the only data-bearing actor RPC."""
        return self.__collector.ingest(wire)

    def disable(self) -> None:
        """Disable the collector without exposing a data-mutation control."""
        self.__collector.disable()

    def snapshot(self) -> dict[str, Any]:
        """Return one detached schema-v2 snapshot and bounded diagnostics."""
        return self.__collector.snapshot()


_TERMINAL_NODE_STATES = frozenset({"SUCCEEDED", "FAILED"})


def _event_timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
