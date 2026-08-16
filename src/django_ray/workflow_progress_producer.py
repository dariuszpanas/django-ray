"""Bound one leaf-local workflow application-progress producer."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any

from django_ray.workflow.progress.limits import (
    WORKFLOW_PROGRESS_LIMITS_V1,
    WorkflowProgressLimits,
)
from django_ray.workflow_progress_protocol import (
    WorkflowProgressEventKind,
    prepare_workflow_progress_event,
)


class WorkflowProgressProducerAck(StrEnum):
    """One non-blocking observation of an actor-ingest acknowledgement."""

    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"
    ACTOR_REJECTED = "actor_rejected"
    ACK_FAILED = "ack_failed"


WorkflowProgressAckPoller = Callable[[Any], WorkflowProgressProducerAck]

_TERMINAL_HANDOFF_NOT_NEEDED = "not_needed"
_TERMINAL_HANDOFF_SUBMITTED = "submitted"
_TERMINAL_HANDOFF_FAILED = "failed"
_TERMINAL_HANDOFF_ACTOR_UNAVAILABLE = "actor_unavailable"


def _poll_ray_ack(reference: Any) -> WorkflowProgressProducerAck:
    """Poll one Ray ObjectRef without waiting for actor progress."""
    try:
        import ray

        ready, _ = ray.wait([reference], num_returns=1, timeout=0)
        if not ready:
            return WorkflowProgressProducerAck.PENDING
        accepted = ray.get(ready[0])
    except Exception:
        return WorkflowProgressProducerAck.ACK_FAILED
    return (
        WorkflowProgressProducerAck.ACKNOWLEDGED
        if accepted is True
        else WorkflowProgressProducerAck.ACTOR_REJECTED
    )


class WorkflowProgressProducerSession:
    """Coalesce one leaf's replaceable updates behind one outstanding call."""

    __slots__ = (
        "_ack_poller",
        "_actor",
        "_available",
        "_counter_max",
        "_counters",
        "_finished_report",
        "_limits",
        "_node_id",
        "_outstanding",
        "_pending_wire",
        "_run_identity",
        "_saturated",
        "_terminal_handoff",
    )

    def __init__(
        self,
        actor: Any,
        run_identity: dict[str, Any],
        node_id: str,
        *,
        limits: WorkflowProgressLimits = WORKFLOW_PROGRESS_LIMITS_V1,
        ack_poller: WorkflowProgressAckPoller | None = None,
    ) -> None:
        self._actor = actor
        self._run_identity = dict(run_identity)
        self._node_id = node_id
        self._limits = limits
        self._counter_max = limits.identity_max_integer
        self._ack_poller = _poll_ray_ack if ack_poller is None else ack_poller
        self._outstanding: Any | None = None
        self._pending_wire: bytes | None = None
        self._available = True
        self._saturated = False
        self._terminal_handoff = _TERMINAL_HANDOFF_NOT_NEEDED
        self._finished_report: dict[str, Any] | None = None
        self._counters = {
            "offered": 0,
            "submitted": 0,
            "superseded": 0,
            "locally_dropped": 0,
            "acknowledged": 0,
            "actor_rejected": 0,
            "ack_failed": 0,
        }

    def _increment(self, name: str, amount: int = 1) -> None:
        current = self._counters[name]
        if current >= self._counter_max or amount > self._counter_max - current:
            self._counters[name] = self._counter_max
            self._saturated = True
            return
        self._counters[name] = current + amount

    def _observe_outstanding(self) -> bool:
        """Return whether the tracked mutable call remains pending."""
        if self._outstanding is None:
            return False
        try:
            status = WorkflowProgressProducerAck(self._ack_poller(self._outstanding))
        except Exception:
            status = WorkflowProgressProducerAck.ACK_FAILED
        if status is WorkflowProgressProducerAck.PENDING:
            return True
        self._outstanding = None
        if status is WorkflowProgressProducerAck.ACKNOWLEDGED:
            self._increment("acknowledged")
        elif status is WorkflowProgressProducerAck.ACTOR_REJECTED:
            self._increment("actor_rejected")
            self._available = False
        else:
            self._increment("ack_failed")
            self._available = False
        return False

    def _submit(self, wire: bytes, *, retain_ack: bool) -> bool:
        try:
            reference = self._actor.ingest.remote(wire)
        except Exception:
            self._increment("locally_dropped")
            self._available = False
            return False
        self._increment("submitted")
        if reference is None:
            # Test doubles and in-process adapters may complete synchronously.
            self._increment("acknowledged")
        elif retain_ack:
            self._outstanding = reference
        return True

    def offer(
        self,
        current: int | float,
        total: int | float,
        *,
        message: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> bool:
        """Validate and submit or coalesce one latest application-progress value."""
        if self._finished_report is not None:
            return False
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
        self._increment("offered")

        outstanding_pending = self._observe_outstanding()
        if self._pending_wire is not None:
            self._increment("superseded")
        self._pending_wire = wire

        if outstanding_pending:
            return True
        candidate = self._pending_wire
        self._pending_wire = None
        if not self._available:
            self._increment("locally_dropped")
            return False
        if candidate is None:  # pragma: no cover - assignment invariant
            raise AssertionError("workflow progress producer lost its pending wire")
        return self._submit(candidate, retain_ack=True)

    def finish(self) -> dict[str, Any]:
        """Seal the producer and hand off at most one latest buffered value."""
        if self._finished_report is not None:
            return dict(self._finished_report)

        self._observe_outstanding()
        pending_wire = self._pending_wire
        self._pending_wire = None
        if pending_wire is not None:
            if not self._available:
                self._increment("locally_dropped")
                self._terminal_handoff = _TERMINAL_HANDOFF_ACTOR_UNAVAILABLE
            elif self._submit(pending_wire, retain_ack=False):
                self._terminal_handoff = _TERMINAL_HANDOFF_SUBMITTED
            else:
                self._terminal_handoff = _TERMINAL_HANDOFF_FAILED

        pending_acknowledgements = max(
            0,
            self._counters["submitted"]
            - self._counters["acknowledged"]
            - self._counters["actor_rejected"]
            - self._counters["ack_failed"],
        )
        if pending_acknowledgements > self._counter_max:
            pending_acknowledgements = self._counter_max
            self._saturated = True
        self._outstanding = None
        report = {
            "schema_version": 1,
            "saturated": self._saturated,
            **self._counters,
            "pending_acknowledgements": pending_acknowledgements,
            "terminal_handoff": self._terminal_handoff,
        }
        self._finished_report = report
        return dict(report)


__all__ = [
    "WorkflowProgressAckPoller",
    "WorkflowProgressProducerAck",
    "WorkflowProgressProducerSession",
]
