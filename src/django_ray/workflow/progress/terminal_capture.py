"""Capture one bounded final progress value without a Ray actor handle."""

from __future__ import annotations

from threading import Lock
from typing import Any

from django_ray.workflow.progress.limits import WorkflowProgressLimits
from django_ray.workflow.progress.protocol import (
    WorkflowProgressEventKind,
    prepare_workflow_progress_event,
)


class TerminalWorkflowProgressCapture:
    """Share one local latest-value slot across copied invocation contexts.

    No actor handle or transport callback exists here. Every retry invocation
    owns its own capture; only its final Ray result can publish the bounded wire.
    """

    def __init__(
        self,
        run_identity: dict[str, Any],
        node_id: str,
        *,
        limits: WorkflowProgressLimits,
    ) -> None:
        self._run_identity = dict(run_identity)
        self._node_id = node_id
        self._limits = limits
        self._lock = Lock()
        self._wire: bytes | None = None
        self._sealed = False
        self._saturated = False
        self._counters = dict.fromkeys(
            ("offered", "accepted", "rejected", "superseded", "canonical_bytes"), 0
        )

    def _increment(self, name: str, amount: int = 1) -> None:
        total = self._counters[name] + amount
        maximum = self._limits.identity_max_integer
        if total > maximum:
            self._saturated = True
        self._counters[name] = min(total, maximum)

    def offer(
        self,
        current: int | float,
        total: int | float,
        *,
        message: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> bool:
        with self._lock:
            if self._sealed:
                return False
            self._increment("offered")
            try:
                wire = prepare_workflow_progress_event(
                    self._run_identity,
                    WorkflowProgressEventKind.APPLICATION_PROGRESS,
                    {
                        "node_id": self._node_id,
                        "current": float(current),
                        "total": float(total),
                        "message": message,
                        "metrics": {} if metrics is None else metrics,
                    },
                    limits=self._limits,
                )
            except (TypeError, ValueError, OverflowError):
                self._increment("rejected")
                raise
            self._increment("accepted")
            self._increment("canonical_bytes", len(wire))
            if self._wire is not None:
                self._increment("superseded")
            self._wire = wire
            return True

    def finish(self) -> dict[str, Any]:
        with self._lock:
            self._sealed = True
            return {
                "schema_version": 1,
                "transport": "terminal_capture",
                **self._counters,
                "retained": int(self._wire is not None),
                "retained_bytes": len(self._wire) if self._wire is not None else 0,
                "saturated": self._saturated,
            }

    def terminal_progress_wire(self) -> bytes | None:
        with self._lock:
            if not self._sealed:
                raise RuntimeError("Workflow progress capture is not sealed")
            return self._wire
